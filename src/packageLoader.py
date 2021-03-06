"""
Handles scanning through the zip packages to find all items, styles, etc.
"""
import operator
import os
import os.path
import shutil
from collections import defaultdict
from contextlib import ExitStack
from zipfile import ZipFile

import extract_packages
import srctools
import tkMarkdown
import utils
from FakeZip import FakeZip, zip_names, zip_open_bin
from loadScreen import main_loader as loader
from packageMan import PACK_CONFIG
from selectorWin import SelitemData
from srctools import (
    Property, NoKeyError,
    Vec, EmptyMapping,
    VMF, Entity, Solid,
    VPK,
)

from typing import (
    Union, Optional, Any, TYPE_CHECKING,
    Iterator, Iterable, Type,
    Dict, List, Tuple, NamedTuple,
)

if TYPE_CHECKING:
    from gameMan import Game

LOGGER = utils.getLogger(__name__)

all_obj = {}
obj_override = {}
packages = {}  # type: Dict[str, Package]
OBJ_TYPES = {}

data = {}

res_count = -1

# Don't change face IDs when copying to here.
# This allows users to refer to the stuff in templates specifically.
# The combined VMF isn't to be compiled or edited outside of us, so it's fine
# to have overlapping IDs between templates.
TEMPLATE_FILE = VMF(preserve_ids=True)

# Various namedtuples to allow passing blocks of data around
# (especially to functions that only use parts.)

# Tempory data stored when parsing info.txt, but before .parse() is called.
# This allows us to parse all packages before loading objects.
ObjData = NamedTuple('ObjData', [
    ('zip_file', Union[ZipFile, FakeZip]),
    ('info_block', Property),
    ('pak_id', str),
    ('disp_name', str),
])
# The arguments for pak_object.parse().
ParseData = NamedTuple('ParseData', [
    ('zip_file', Union[ZipFile, FakeZip]),
    ('id', str),
    ('info', Property),
    ('pak_id', str),
    ('is_override', bool),
])
# The values stored for OBJ_TYPES
ObjType = NamedTuple('ObjType', [
    ('cls', Type['PakObject']),
    ('allow_mult', bool),
    ('has_img', bool),
])
# The arguments to pak_object.export().
ExportData = NamedTuple('ExportData', [
    ('selected', str),
    ('selected_style', 'Style'),  # Some items need to know which style is selected
    ('editoritems', Property),
    ('vbsp_conf', Property),
    ('game', 'Game'),
])

# This package contains necessary components, and must be available.
CLEAN_PACKAGE = 'BEE2_CLEAN_STYLE'

# Check to see if the zip contains the resources referred to by the packfile.
CHECK_PACKFILE_CORRECTNESS = False

VPK_OVERRIDE_README = """\
Files in this folder will be written to the VPK during every BEE2 export.
Use to override resources as you please.
"""

# The folder we want to copy our VPKs to.
VPK_FOLDER = {
    # The last DLC released by Valve - this is the one that we
    # overwrite with a VPK file.
    utils.STEAM_IDS['PORTAL2']: 'portal2_dlc3',
    utils.STEAM_IDS['DEST_AP']: 'portal2_dlc3',

    # This doesn't have VPK files, and is higher priority.
    utils.STEAM_IDS['APERTURE TAG']: 'portal2',
}


class _PakObjectMeta(type):
    def __new__(mcs, name, bases, namespace, allow_mult=False, has_img=True):
        """Adds a PakObject to the list of objects.

        Making a metaclass allows us to hook into the creation of all subclasses.
        """
        # Defer to type to create the class..
        cls = type.__new__(mcs, name, bases, namespace)

        # Only register subclasses of PakObject - those with a parent class.
        # PakObject isn't created yet so we can't directly check that.
        if bases:
            OBJ_TYPES[name] = ObjType(cls, allow_mult, has_img)

        # Maps object IDs to the object.
        cls._id_to_obj = {}

        return cls

    def __init__(cls, name, bases, namespace, **kwargs):
        # We have to strip kwargs from the type() calls to prevent errors.
        type.__init__(cls, name, bases, namespace)


class PakObject(metaclass=_PakObjectMeta):
    """PackObject(allow_mult=False, has_img=True): The base class for package objects.

    In the class base list, set 'allow_mult' to True if duplicates are allowed.
    If duplicates occur, they will be treated as overrides.
    Set 'has_img' to control whether the object will count towards the images
    loading bar - this should be stepped in the UI.load_packages() method.
    """
    @classmethod
    def parse(cls, data: ParseData) -> 'PakObject':
        """Parse the package object from the info.txt block.

        ParseData is a namedtuple containing relevant info:
        - zip_file, the package's ZipFile or FakeZip
        - id, the ID of the item
        - info, the Property block in info.txt
        - pak_id, the ID of the package
        """
        raise NotImplementedError

    def add_over(self, override: 'PakObject'):
        """Called to override values.
        self is the originally defined item, and override is the override item
        to copy values from.
        """
        pass

    @staticmethod
    def export(exp_data: ExportData):
        """Export the appropriate data into the game.

        ExportData is a namedtuple containing various data:
        - selected: The ID of the selected item (or None)
        - selected_style: The selected style object
        - editoritems: The Property block for editoritems.txt
        - vbsp_conf: The Property block for vbsp_config
        - game: The game we're exporting to.
        """
        raise NotImplementedError

    @classmethod
    def all(cls: _PakObjectMeta) -> Iterable['PakObject']:
        """Get the list of objects parsed."""
        return cls._id_to_obj.values()

    @classmethod
    def by_id(cls: _PakObjectMeta, object_id: str) -> 'PakObject':
        """Return the object with a given ID."""
        return cls._id_to_obj[object_id.casefold()]


def reraise_keyerror(err, obj_id):
    """Replace NoKeyErrors with a nicer one, giving the item that failed."""
    if isinstance(err, IndexError):
        if isinstance(err.__cause__, NoKeyError):
            # Property.__getitem__ raises IndexError from
            # NoKeyError, so read from the original
            key_error = err.__cause__
        else:
            # We shouldn't have caught this
            raise err
    else:
        key_error = err
    raise Exception(
        'No "{key}" in {id!s} object!'.format(
            key=key_error.key,
            id=obj_id,
        )
    ) from err


def get_config(
        prop_block: Property,
        zip_file,
        folder: str,
        pak_id='',
        prop_name='config',
        extension='.cfg',
        ):
    """Extract a config file refered to by the given property block.

    Looks for the prop_name key in the given prop_block.
    If the keyvalue has a value of "", an empty tree is returned.
    If it has children, a copy of them is returned.
    Otherwise the value is a filename in the zip which will be parsed.
    """
    prop_block = prop_block.find_key(prop_name, "")
    if prop_block.has_children():
        prop = prop_block.copy()
        prop.name = None
        return prop

    if prop_block.value == '':
        return Property(None, [])

    # Zips must use '/' for the seperator, even on Windows!
    path = folder + '/' + prop_block.value
    if len(path) < 3 or path[-4] != '.':
        # Add extension
        path += extension
    try:
        with zip_file.open(path) as f:
            return Property.parse(
                f,
                pak_id + ':' + path,
            )
    except KeyError:
        LOGGER.warning('"{id}:{path}" not in zip!', id=pak_id, path=path)
        return Property(None, [])
    except UnicodeDecodeError:
        LOGGER.exception('Unable to read "{id}:{path}"', id=pak_id, path=path)
        raise


def set_cond_source(props: Property, source: str):
    """Set metadata for Conditions in the given config blocks.

    This generates '__src__' keyvalues in Condition blocks with info like
    the source object ID and originating file, so errors can be traced back
    to the config file creating it.
    """
    for cond in props.find_all('Conditions', 'Condition'):
        cond['__src__'] = source


def find_packages(pak_dir, zips, zip_stack: ExitStack, zip_name_lst):
    """Search a folder for packages, recursing if necessary."""
    found_pak = False
    for name in os.listdir(pak_dir):  # Both files and dirs
        name = os.path.join(pak_dir, name)
        is_dir = os.path.isdir(name)
        if name.endswith('.zip') and os.path.isfile(name):
            zip_file = ZipFile(name)
            # Ensure we quit close this zipfile..
            zip_stack.enter_context(zip_file)
        elif is_dir:
            zip_file = FakeZip(name)
            # FakeZips don't actually hold a file handle, we don't need to
            # close them.
        else:
            LOGGER.info('Extra file: {}', name)
            continue

        LOGGER.debug('Reading package "' + name + '"')

        try:
            # Valid packages must have an info.txt file!
            info_file = zip_file.open('info.txt')
        except KeyError:
            if is_dir:
                # This isn't a package, so check the subfolders too...
                LOGGER.debug('Checking subdir "{}" for packages...', name)
                find_packages(name, zips, zip_stack, zip_name_lst)
            else:
                # Invalid, explicitly close this zipfile handle..
                zip_file.close()
                LOGGER.warning('ERROR: Bad package "{}"!', name)
        else:
            with info_file:
                info = Property.parse(info_file, name + ':info.txt')

            # Add the zipfile to the list, it's valid
            zips.append(zip_file)
            zip_name_lst.append(os.path.abspath(name))

            pak_id = info['ID']
            packages[pak_id] = Package(
                pak_id,
                zip_file,
                info,
                name,
            )
            found_pak = True

    if not found_pak:
        LOGGER.debug('No packages in folder!')


def load_packages(
        pak_dir,
        log_item_fallbacks=False,
        log_missing_styles=False,
        log_missing_ent_count=False,
        log_incorrect_packfile=False,
        has_mel_music=False,
        has_tag_music=False,
        ):
    """Scan and read in all packages in the specified directory."""
    global LOG_ENT_COUNT, CHECK_PACKFILE_CORRECTNESS
    pak_dir = os.path.abspath(os.path.join(os.getcwd(), '..', pak_dir))

    if not os.path.isdir(pak_dir):
        from tkinter import messagebox
        import sys
        # We don't have a packages directory!
        messagebox.showerror(
            master=loader,
            title='BEE2 - Invalid Packages Directory!',
            message='The given packages directory is not present!\n'
                    'Get the packages from '
                    '"http://github.com/BEEmod/BEE2-items" '
                    'and place them in "' + pak_dir +
                    os.path.sep + '".',
                    # Add slash to the end to indicate it's a folder.
        )
        sys.exit('No Packages Directory!')

    shutil.rmtree('../vpk_cache/', ignore_errors=True)

    LOG_ENT_COUNT = log_missing_ent_count
    CHECK_PACKFILE_CORRECTNESS = log_incorrect_packfile
    zips = []
    data['zips'] = []

    # Use ExitStack to dynamically manage the zipfiles we find and open.
    with ExitStack() as zip_stack:
        find_packages(pak_dir, zips, zip_stack, data['zips'])

        pack_count = len(packages)
        loader.set_length("PAK", pack_count)

        for obj_type in OBJ_TYPES:
            all_obj[obj_type] = {}
            obj_override[obj_type] = defaultdict(list)
            data[obj_type] = []

        images = 0
        for pak_id, pack in packages.items():
            if not pack.enabled:
                LOGGER.info('Package {id} disabled!', id=pak_id)
                pack_count -= 1
                loader.set_length("PAK", pack_count)
                continue

            LOGGER.info('Reading objects from "{id}"...', id=pak_id)
            img_count = parse_package(pack, has_tag_music, has_mel_music)
            images += img_count
            loader.step("PAK")

        # If new packages were added, update the config!
        PACK_CONFIG.save_check()

        loader.set_length("OBJ", sum(
            len(obj_type)
            for obj_type in
            all_obj.values()
        ))
        loader.set_length("IMG_EX", images)

        # The number of images we need to load is the number of objects,
        # excluding some types like Stylevars or PackLists.
        loader.set_length(
            "IMG",
            sum(
                len(all_obj[key])
                for key, opts in
                OBJ_TYPES.items()
                if opts.has_img
            )
        )

        for obj_type, objs in all_obj.items():
            for obj_id, obj_data in objs.items():
                LOGGER.debug('Loading {type} "{id}"!', type=obj_type, id=obj_id)
                obj_class = OBJ_TYPES[obj_type].cls  # type: Type[PakObject]
                # parse through the object and return the resultant class
                try:
                    object_ = obj_class.parse(
                        ParseData(
                            obj_data.zip_file,
                            obj_id,
                            obj_data.info_block,
                            obj_data.pak_id,
                            False,
                        )
                    )
                except (NoKeyError, IndexError) as e:
                    reraise_keyerror(e, obj_id)

                if not hasattr(object_, 'id'):
                    raise ValueError(
                        '"{}" object {} has no ID!'.format(obj_type, object_)
                    )

                obj_class._id_to_obj[object_.id.casefold()] = object_

                object_.pak_id = obj_data.pak_id
                object_.pak_name = obj_data.disp_name
                for override_data in obj_override[obj_type].get(obj_id, []):
                    override = OBJ_TYPES[obj_type].cls.parse(
                        override_data
                    )
                    object_.add_over(override)
                data[obj_type].append(object_)
                loader.step("OBJ")

        # Extract all resources/BEE2/ images.

        img_dest = '../images/cache'

        shutil.rmtree(img_dest, ignore_errors=True)
        img_loc = os.path.join('resources', 'bee2')
        for zip_file in zips:
            for path in zip_names(zip_file):
                loc = os.path.normcase(path).casefold()
                if not loc.startswith(img_loc):
                    continue
                # Strip resources/BEE2/ from the path and move to the
                # cache folder.
                dest_loc = os.path.join(
                    img_dest,
                    os.path.relpath(loc, img_loc)
                )
                # Make the destination directory and copy over the image
                os.makedirs(os.path.dirname(dest_loc), exist_ok=True)
                with zip_open_bin(zip_file, path) as src:
                    with open(dest_loc, mode='wb') as dest:
                        shutil.copyfileobj(src, dest)
                loader.step("IMG_EX")

    LOGGER.info('Allocating styled items...')
    setup_style_tree(
        Item.all(),
        Style.all(),
        log_item_fallbacks,
        log_missing_styles,
    )
    return data


def parse_package(pack: 'Package', has_tag=False, has_mel=False):
    """Parse through the given package to find all the components."""
    for pre in Property.find_key(pack.info, 'Prerequisites', []):
        # Special case - disable these packages when the music isn't copied.
        if pre.value == '<TAG_MUSIC>':
            if not has_tag:
                return 0
        elif pre.value == '<MEL_MUSIC>':
            if not has_mel:
                return 0
        elif pre.value not in packages:
            LOGGER.warning(
                'Package "{pre}" required for "{id}" - '
                'ignoring package!',
                pre=pre.value,
                id=pack.id,
            )
            return 0

    # First read through all the components we have, so we can match
    # overrides to the originals
    for comp_type in OBJ_TYPES:
        allow_dupes = OBJ_TYPES[comp_type].allow_mult
        # Look for overrides
        for obj in pack.info.find_all("Overrides", comp_type):
            obj_id = obj['id']
            obj_override[comp_type][obj_id].append(
                ParseData(pack.zip, obj_id, obj, pack.id, True)
            )

        for obj in pack.info.find_all(comp_type):
            obj_id = obj['id']
            if obj_id in all_obj[comp_type]:
                if allow_dupes:
                    # Pretend this is an override
                    obj_override[comp_type][obj_id].append(
                        ParseData(pack.zip, obj_id, obj, pack.id, True)
                    )
                    # Don't continue to parse and overwrite
                    continue
                else:
                    raise Exception('ERROR! "' + obj_id + '" defined twice!')
            all_obj[comp_type][obj_id] = ObjData(
                pack.zip,
                obj,
                pack.id,
                pack.disp_name,
            )

    img_count = 0
    img_loc = os.path.join('resources', 'bee2')
    for item in zip_names(pack.zip):
        item = os.path.normcase(item).casefold()
        if item.startswith("resources"):
            extract_packages.res_count += 1
            if item.startswith(img_loc):
                img_count += 1
    return img_count


def setup_style_tree(
    item_data: Iterable['Item'],
    style_data: Iterable['Style'],
    log_fallbacks,
    log_missing_styles,
):
    """Modify all items so item inheritance is properly handled.

    This will guarantee that all items have a definition for each
    combination of item and version.
    The priority is:
    - Exact Match
    - Parent style
    - Grandparent (etc) style
    - First version's style
    - First style of first version
    """
    all_styles = {}  # type: Dict[str, Style]

    for style in style_data:
        all_styles[style.id] = style

    for style in all_styles.values():
        base = []
        b_style = style
        while b_style is not None:
            # Recursively find all the base styles for this one

            if b_style in base:
                # Already hit this!
                raise Exception('Loop in bases for "{}"!'.format(b_style.id))
            base.append(b_style)
            b_style = all_styles.get(b_style.base_style, None)
            # Just append the style.base_style to the list,
            # until the style with that ID isn't found anymore.
        style.bases = base

    # All styles now have a .bases attribute, which is a list of the
    # parent styles that exist (plus the style).

    # To do inheritance, we simply copy the data to ensure all items
    # have data defined for every used style.
    for item in item_data:
        all_ver = list(item.versions.values())  # type: List[Dict[str, Union[Dict[str, Style], str]]]
        # Move default version to the beginning, so it's read first
        all_ver.remove(item.def_ver)
        all_ver.insert(0, item.def_ver)
        for vers in all_ver:
            for sty_id, style in all_styles.items():
                if sty_id in vers['styles']:
                    continue  # We already have a definition, or a reference
                for base_style in style.bases:
                    if base_style.id in vers['styles']:
                        # Copy the values for the parent to the child style
                        vers['styles'][sty_id] = vers['styles'][base_style.id]
                        if log_fallbacks and not item.unstyled:
                            LOGGER.warning(
                                'Item "{item}" using parent '
                                '"{rep}" for "{style}"!',
                                item=item.id,
                                rep=base_style.id,
                                style=sty_id,
                            )
                        break
                else:
                    # For the base version, use the first style if
                    # a styled version is not present
                    if vers['id'] == item.def_ver['id']:
                        vers['styles'][sty_id] = vers['styles'][vers['def_style']]
                        if log_missing_styles and not item.unstyled:
                            LOGGER.warning(
                                'Item "{item}" using '
                                'inappropriate style for "{style}"!',
                                item=item.id,
                                style=sty_id,
                            )
                    else:
                        # For versions other than the first, use
                        # the base version's definition
                        vers['styles'][sty_id] = item.def_ver['styles'][sty_id]

            style_lookups = {}

            # Evaluate style lookups and modifications
            for sty_id, props in vers['styles'].items():
                if not isinstance(props, Property):
                    continue  # Normal value
                if props.name is None:
                    # Style lookup
                    style_lookups[sty_id] = props.value
                    continue
                # It's a reference to another style.
                base = props['base', '']
                if not base:
                    raise Exception('No base for "{}", in "{}" style.'.format(
                        item.id, sty_id,
                    ))

                try:
                    base_variant = vers['styles'][base]  # type: ItemVariant
                except KeyError:
                    raise Exception(
                        'Invalid style base '
                        '("{}") for "{}", in "{}" style.'.format(
                            base, item.id, sty_id,
                        )
                    )

                vers['styles'][sty_id] = base_variant.modify(
                    props,
                    '<{}:{}.{}>'.format(item.id, vers['id'], sty_id)
                )

            for to_id, from_id in style_lookups.items():
                LOGGER.warning('REF "{}": {} -> {}', item.id, from_id, to_id)
                try:
                    vers['styles'][to_id] = vers['styles'][from_id]
                except KeyError:
                    raise Exception(
                        'Invalid style reference '
                        '("{}") for "{}", in "{}" style.'.format(
                            from_id, item.id, to_id,
                        )
                    )

            # The default style is a value reference, fix it up.
            # If it's an invalid value the above loop will have caught
            # that, since it already read the value.
            vers['def_style'] = vers['styles'][vers['def_style']]


def parse_item_folder(folders: Dict[str, Any], zip_file, pak_id):
    """Parse through the data in item/ folders.

    folders is a dict, with the keys set to the folder names we want.
    The values will be filled in with itemVariant values
    """
    for fold in folders:
        prop_path = 'items/' + fold + '/properties.txt'
        editor_path = 'items/' + fold + '/editoritems.txt'
        config_path = 'items/' + fold + '/vbsp_config.cfg'
        try:
            with zip_file.open(prop_path, 'r') as prop_file:
                props = Property.parse(
                    prop_file, pak_id + ':' + prop_path,
                ).find_key('Properties')
            with zip_file.open(editor_path, 'r') as editor_file:
                editor = Property.parse(
                    editor_file, pak_id + ':' + editor_path
                )
        except KeyError as err:
            # Opening the files failed!
            raise IOError(
                '"' + pak_id + ':items/' + fold + '" not valid!'
                'Folder likely missing! '
            ) from err

        editor_iter = Property.find_all(editor, 'Item')
        folders[fold] = ItemVariant(
            # The first Item block found
            editoritems=next(editor_iter),
            # Any extra blocks (offset catchers, extent items)
            editor_extra=editor_iter,

            # Add the folder the item definition comes from,
            # so we can trace it later for debug messages.
            source='<{}>/items/{}'.format(pak_id, fold),
            vbsp_config=Property(None, []),

            authors=sep_values(props['authors', '']),
            tags=sep_values(props['tags', '']),
            desc=desc_parse(props, pak_id + ':' + prop_path),
            ent_count=props['ent_count', ''],
            url=props['infoURL', None],
            icons={
                p.name: p.value
                for p in
                props['icon', []]
            },
            all_name=props['all_name', None],
            all_icon=props['all_icon', None],
        )

        if LOG_ENT_COUNT and not folders[fold].ent_count:
            LOGGER.warning(
                '"{id}:{path}" has missing entity count!',
                id=pak_id,
                path=prop_path,
            )

        # If we have at least 1, but not all of the grouping icon
        # definitions then notify the author.
        num_group_parts = (
            (folders[fold].all_name is not None)
            + (folders[fold].all_icon is not None)
            + ('all' in folders[fold].icons)
        )
        if 0 < num_group_parts < 3:
            LOGGER.warning(
                'Warning: "{id}:{path}" has incomplete grouping icon '
                'definition!',
                id=pak_id,
                path=prop_path,
            )
        try:
            with zip_file.open(config_path, 'r') as vbsp_config:
                folders[fold].vbsp_config = conf = Property.parse(
                    vbsp_config,
                    pak_id + ':' + config_path,
                )
        except KeyError:
            folders[fold].vbsp_config = conf = Property(None, [])

        set_cond_source(conf, folders[fold].source)


class ItemVariant:
    """Data required for an item in a particular style."""

    def __init__(
            self,
            editoritems: Property,
            vbsp_config: Property,
            editor_extra: Iterable[Property],
            authors: List[str],
            tags: List[str],
            desc: tkMarkdown.MarkdownData,
            icons: Dict[str, str],
            ent_count: str='',
            url: str = None,
            all_name: str=None,
            all_icon: str=None,
            source: str='',
    ):
        self.editor = editoritems
        self.editor_extra = Property(None, list(editor_extra))
        self.vbsp_config = vbsp_config
        self.source = source  # Original location of configs

        self.authors = authors
        self.tags = tags
        self.desc = desc
        self.icons = icons
        self.ent_count = ent_count
        self.url = url

        # The name and VTF for grouped items
        self.all_name = all_name
        self.all_icon = all_icon

    def can_group(self):
        """Does this variant have the data needed to group?"""
        return (
            'all' in self.icons and
            self.all_icon is not None and
            self.all_name is not None
        )

    def override_from_folder(self, other: 'ItemVariant'):
        """Perform the override from another item folder."""
        self.authors.extend(other.authors)
        self.tags.extend(self.tags)
        self.vbsp_config += other.vbsp_config
        self.desc = tkMarkdown.join(self.desc, other.desc)

    def modify(self, props: Property, source: str) -> 'ItemVariant':
        """Apply a config to this item variant.

        This produces a copy with various modifications - switching
        out palette or instance values, changing the config, etc.
        """
        if 'config' in props:
            # Item.parse() has resolved this to the actual config.
            vbsp_config = props.find_key('config').copy()
            # Specify this is a collection of blocks, not a "config"
            # block.
            vbsp_config.name = None
        else:
            vbsp_config = self.vbsp_config.copy()

        if 'description' in props:
            desc = desc_parse(props, source)
        else:
            desc = self.desc.copy()

        if 'authors' in props:
            authors = sep_values(props['authors', ''])
        else:
            authors = self.authors

        if 'tags' in props:
            tags = sep_values(props['tags', ''])
        else:
            tags = self.tags.copy()

        variant = ItemVariant(
            self.editor.copy(),
            vbsp_config,
            self.editor_extra.copy(),
            authors=authors,
            tags=tags,
            desc=desc,
            icons=self.icons.copy(),
            ent_count=props['ent_count', self.ent_count],
            url=props['url', self.url],
            all_name=self.all_name,
            all_icon=self.all_icon,
            source='{} from {}'.format(source, self.source),
        )
        subtypes = list(variant.editor.find_all('Editor', 'SubType'))
        # Implement overriding palette items
        for item in props.find_children('Palette'):
            pal_icon = item['icon', None]
            pal_name = item['pal_name', None]  # Name for the palette icon
            bee2_icon = item['bee2', None]
            if item.name == 'all':
                variant.all_icon = pal_icon
                variant.all_name = pal_name
                if bee2_icon:
                    variant.icons['all'] = bee2_icon
                continue

            try:
                subtype = subtypes[int(item.name)]
            except (IndexError, ValueError, TypeError):
                raise Exception(
                    'Invalid index "{}" when modifying '
                    'editoritems for {}'.format(item.name, source)
                )

            # Overriding model data
            try:
                try:
                    model_prop = item.find_key('Models')
                except NoKeyError:
                    model_prop = item.find_key('Model')
            except NoKeyError:
                pass
            else:
                while 'model' in subtype:
                    del subtype['model']
                if model_prop.has_children():
                    models = [prop.value for prop in model_prop]
                else:
                    models = [model_prop.value]
                for model in models:
                    subtype.append(Property('Model', [
                        Property('ModelName', model),
                    ]))

            if item['name', None]:
                subtype['name'] = item['name']  # Name for the subtype

            if bee2_icon:
                print(item.name, variant.icons)
                variant.icons[item.name] = bee2_icon

            if pal_name or pal_icon:
                palette = subtype.ensure_exists('Palette')
                if pal_name:
                    palette['Tooltip'] = pal_name
                if pal_icon:
                    palette['Image'] = pal_icon

        # Allow overriding the instance blocks.
        instances = variant.editor.ensure_exists('Exporting').ensure_exists('Instances')
        for inst in props.find_children('Instances'):
            try:
                del instances[inst.real_name]
            except IndexError:
                pass
            if inst.has_children() or not inst.name.isdecimal():
                instances.append(inst.copy())
            else:
                # Shortcut to just create the property
                instances += Property(inst.real_name, [
                    Property('Name', inst.value),
                ])

        return variant


class Package:
    """Represents a package."""
    def __init__(
            self,
            pak_id: str,
            zip_file: ZipFile,
            info: Property,
            name: str,
            ):
        disp_name = info['Name', None]
        if disp_name is None:
            LOGGER.warning('Warning: {id} has no display name!', id=pak_id)
            disp_name = pak_id.lower()

        self.id = pak_id
        self.zip = zip_file
        self.info = info
        self.name = name
        self.disp_name = disp_name
        self.desc = info['desc', '']

    @property
    def enabled(self):
        """Should this package be loaded?"""
        if self.id == CLEAN_PACKAGE:
            # The clean style package is special!
            # It must be present.
            return True

        return PACK_CONFIG.get_bool(self.id, 'Enabled', default=True)

    def set_enabled(self, value: bool):
        if self.id == CLEAN_PACKAGE:
            raise ValueError('The Clean Style package cannot be disabled!')

        PACK_CONFIG[self.id]['Enabled'] = srctools.bool_as_int(value)
    enabled = enabled.setter(set_enabled)

    def is_stale(self):
        """Check to see if this package has been modified since the last run."""
        if isinstance(self.zip, FakeZip):
            # unzipped packages are for development, so always extract.
            LOGGER.info('Extracting resources - {} is unzipped!', self.id)
            return True
        last_modtime = PACK_CONFIG.get_int(self.id, 'ModTime', 0)
        zip_modtime = int(os.stat(self.name).st_mtime)

        if zip_modtime != last_modtime:
            LOGGER.info('Package {} is stale! Extracting resources...', self.id)
            return True
        return False

    def set_modtime(self):
        """After the cache has been extracted, set the modification dates
         in the config."""
        if isinstance(self.zip, FakeZip):
            # No modification time
            PACK_CONFIG[self.id]['ModTime'] = '0'
        else:
            PACK_CONFIG[self.id]['ModTime'] = str(int(
                os.stat(self.name).st_mtime
            ))


class Style(PakObject):
    def __init__(
        self,
        style_id,
        selitem_data: 'SelitemData',
        editor,
        config=None,
        base_style=None,
        suggested=None,
        has_video=True,
        vpk_name='',
        corridor_names=EmptyMapping,
    ):
        self.id = style_id
        self.selitem_data = selitem_data
        self.editor = editor
        self.base_style = base_style
        # Set by setup_style_tree() after all objects are read..
        # this is a list of this style, plus parents in order.
        self.bases = []  # type: List[Style]
        self.suggested = suggested or {}
        self.has_video = has_video
        self.vpk_name = vpk_name
        self.corridor_names = {
            'sp_entry': corridor_names.get('sp_entry', Property('', [])),
            'sp_exit':  corridor_names.get('sp_exit', Property('', [])),
            'coop':     corridor_names.get('coop', Property('', [])),
        }
        if config is None:
            self.config = Property(None, [])
        else:
            self.config = config

        set_cond_source(self.config, 'Style <{}>'.format(style_id))

    @classmethod
    def parse(cls, data):
        """Parse a style definition."""
        info = data.info
        selitem_data = get_selitem_data(info)
        base = info['base', '']
        has_video = srctools.conv_bool(
            info['has_video', ''],
            not data.is_override,  # Assume no video for override
        )
        vpk_name = info['vpk_name', ''].casefold()

        sugg = info.find_key('suggested', [])
        if data.is_override:
            # For overrides, we default to no suggestion..
            sugg = (
                sugg['quote', ''],
                sugg['music', ''],
                sugg['skybox', ''],
                sugg['elev', ''],
            )
        else:
            sugg = (
                sugg['quote', '<NONE>'],
                sugg['music', '<NONE>'],
                sugg['skybox', 'SKY_BLACK'],
                sugg['elev', '<NONE>'],
            )

        corridors = info.find_key('corridors', [])
        corridors = {
            'sp_entry': corridors.find_key('sp_entry', []),
            'sp_exit':  corridors.find_key('sp_exit', []),
            'coop':     corridors.find_key('coop', []),
        }

        if base == '':
            base = None
        try:
            folder = 'styles/' + info['folder']
        except IndexError:
            if data.is_override:
                items = Property(None, [])
                vbsp = None
            else:
                raise ValueError('Style missing configuration!')
        else:
            with data.zip_file.open(folder + '/items.txt', 'r') as item_data:
                items = Property.parse(
                    item_data,
                    data.pak_id + ':' + folder + '/items.txt'
                )

            config = folder + '/vbsp_config.cfg'
            try:
                with data.zip_file.open(config, 'r') as vbsp_config:
                    vbsp = Property.parse(
                        vbsp_config,
                        data.pak_id + ':' + config,
                    )
            except KeyError:
                vbsp = None

        return cls(
            style_id=data.id,
            selitem_data=selitem_data,
            editor=items,
            config=vbsp,
            base_style=base,
            suggested=sugg,
            has_video=has_video,
            corridor_names=corridors,
            vpk_name=vpk_name
        )

    def add_over(self, override: 'Style'):
        """Add the additional commands to ourselves."""
        self.editor.append(override.editor)
        self.config.append(override.config)
        self.selitem_data = join_selitem_data(
            self.selitem_data,
            override.selitem_data
        )

        self.has_video = self.has_video or override.has_video
        # If overrides have suggested IDs, use those. Unset values = ''.
        self.suggested = tuple(
            over_sugg or self_sugg
            for self_sugg, over_sugg in
            zip(self.suggested, override.suggested)
        )


    def __repr__(self):
        return '<Style:' + self.id + '>'

    def export(self):
        """Export this style, returning the vbsp_config and editoritems.

        This is a special case, since styles should go first in the lists.
        """
        vbsp_config = Property(None, [])

        # Editoritems.txt is composed of a "ItemData" block, holding "Item" and
        # "Renderables" sections.

        editoritems = Property("ItemData", [])

        # Only add the actual Item blocks,
        # Renderables is added in gameMan specially.
        # It must come last.
        editoritems += self.editor.copy().find_all("Item")
        vbsp_config += self.config.copy()

        return editoritems, vbsp_config


class Item(PakObject):
    """An item in the editor..."""
    def __init__(
            self,
            item_id,
            versions,
            def_version,
            needs_unlock=False,
            all_conf=None,
            unstyled=False,
            glob_desc=(),
            desc_last=False,
            ):
        self.id = item_id
        self.versions = versions
        self.def_ver = def_version
        self.def_data = def_version['def_style']
        self.needs_unlock = needs_unlock
        self.all_conf = all_conf or Property(None, [])
        self.unstyled = unstyled
        self.glob_desc = glob_desc
        self.glob_desc_last = desc_last

    @classmethod
    def parse(cls, data: ParseData):
        """Parse an item definition."""
        versions = {}
        def_version = None
        # The folders we parse for this - we don't want to parse the same
        # one twice. First they're set to True if we need to read them,
        # then parse_item_folder() replaces that with the actual values
        folders = {}  # type: Dict[str, Optional[ItemVariant]
        unstyled = data.info.bool('unstyled')

        glob_desc = desc_parse(data.info, 'global:' + data.id)
        desc_last = data.info.bool('AllDescLast')

        all_config = get_config(
            data.info,
            data.zip_file,
            'items',
            pak_id=data.pak_id,
            prop_name='all_conf',
        )
        set_cond_source(all_config, '<Item {} all_conf>'.format(
            data.id,
        ))

        needs_unlock = data.info.bool('needsUnlock')

        for ver in data.info.find_all('version'):  # type: Property
            vals = {
                'name':    ver['name', 'Regular'],
                'id':      ver['ID', 'VER_DEFAULT'],
                'styles':  {},
                'def_style': None,
                }
            for style in ver.find_children('styles'):
                if style.has_children():
                    # It's a modification to another folder, keep the property.
                    folder = style
                    # Read in the vbsp_config data if specified.
                    # We need to do this here, since the other functions
                    # don't have access to the zip file.
                    if 'config' in folder:
                        folder['config'] = get_config(
                            folder,
                            data.zip_file,
                            'items',
                            data.pak_id,
                        )

                elif style.value.startswith('<') and style.value.endswith('>'):
                    # Reusing another style unaltered using <>.
                    # None signals this should be calculated after the other
                    # modifications
                    folder = Property(None, style.value[1:-1])
                else:
                    # Reference to the actual folder...
                    folder = style.value
                    folders[folder] = None

                # The first style is considered the 'default', and is used
                # if not otherwise present.
                # We set it to the name, then lookup later in setup_style_tree()
                if vals['def_style'] is None:
                    vals['def_style'] = style.real_name
                vals['styles'][style.real_name] = folder
            versions[vals['id']] = vals
            if def_version is None:
                def_version = vals

        # Fill out the folders dict with the actual data
        parse_item_folder(folders, data.zip_file, data.pak_id)

        # Then copy over to the styles values
        for ver in versions.values():
            if ver['def_style'] in folders:
                ver['def_style'] = folders[ver['def_style']]
            for sty, fold in ver['styles'].items():
                if isinstance(fold, str):
                    ver['styles'][sty] = folders[fold]

        if not versions:
            raise ValueError('Item "' + data.id + '" has no versions!')

        return cls(
            data.id,
            versions=versions,
            def_version=def_version,
            needs_unlock=needs_unlock,
            all_conf=all_config,
            unstyled=unstyled,
            glob_desc=glob_desc,
            desc_last=desc_last,
        )

    def add_over(self, override):
        """Add the other item data to ourselves."""
        # Copy over all_conf always.
        self.all_conf += override.all_conf

        for ver_id, version in override.versions.items():
            if ver_id not in self.versions:
                # We don't have that version!
                self.versions[ver_id] = version
            else:
                our_ver = self.versions[ver_id]['styles']
                for sty_id, style in version['styles'].items():
                    if sty_id not in our_ver:
                        # We don't have that style!
                        our_ver[sty_id] = style
                    else:
                        our_style = our_ver[sty_id]  # type: ItemVariant
                        # We both have a matching folder, merge the
                        # definitions. We don't override editoritems!

                        if isinstance(our_style, str) or isinstance(style, str):
                            raise Exception("Can't override with a <STYLE> def.")

                        our_style.override_from_folder(style)

    def __repr__(self):
        return '<Item:' + self.id + '>'

    @staticmethod
    def export(exp_data: ExportData):
        """Export all items into the configs.

        For the selected attribute, this takes a tuple of values:
        (pal_list, versions, prop_conf)
        Pal_list is a list of (item, subitem) tuples representing the palette.
        Versions is a {item:version_id} dictionary.
        prop_conf is a {item_id: {prop_name: value}} nested dictionary for
         overridden property names. Empty dicts can be passed instead.
        """
        editoritems = exp_data.editoritems
        vbsp_config = exp_data.vbsp_conf
        pal_list, versions, prop_conf = exp_data.selected

        style_id = exp_data.selected_style.id

        aux_item_configs = {
            conf.id: conf
            for conf in ItemConfig.all()
        }

        for item in sorted(Item.all(), key=operator.attrgetter('id')):  # type: Item
            ver_id = versions.get(item.id, 'VER_DEFAULT')

            (
                item_block,
                editor_parts,
                config_part
            ) = item._get_export_data(
                pal_list, ver_id, style_id, prop_conf,
            )
            editoritems += item_block.copy()
            editoritems += editor_parts.copy()
            vbsp_config += config_part.copy()

            # Add auxiliary configs as well.
            try:
                aux_conf = aux_item_configs[item.id]  # type: ItemConfig
            except KeyError:
                pass
            else:
                vbsp_config += aux_conf.all_conf.copy()
                try:
                    version_data = aux_conf.versions[ver_id].copy()
                except KeyError:
                    pass  # No override.
                else:
                    # Find the first style definition for the selected one
                    # that's defined for this config
                    for poss_style in exp_data.selected_style.bases:
                        if poss_style.id in version_data:
                            vbsp_config += version_data[poss_style.id].copy()
                            break

    def _get_export_data(
        self,
        pal_list,
        ver_id,
        style_id,
        prop_conf: Dict[str, Dict[str, str]],
    ) -> Tuple[Property, Property, Property]:
        """Get the data for an exported item."""

        # Build a dictionary of this item's palette positions,
        # if any exist.
        palette_items = {
            subitem: index
            for index, (item, subitem) in
            enumerate(pal_list)
            if item == self.id
        }

        item_data = self.versions[ver_id]['styles'][style_id]  # type: ItemVariant

        new_editor = item_data.editor.copy()

        new_editor['type'] = self.id  # Set the item ID to match our item
        # This allows the folders to be reused for different items if needed.

        for index, editor_section in enumerate(
                new_editor.find_all("Editor", "Subtype")):

            # For each subtype, see if it's on the palette
            for editor_sec_index, pal_section in enumerate(
                    editor_section):
                # We need to manually loop so we get the index of the palette
                # property block in the section
                if pal_section.name != "palette":
                    # Skip non-palette blocks in "SubType"
                    # (animations, sounds, model)
                    continue

                if index in palette_items:
                    if len(palette_items) == 1:
                        # Switch to the 'Grouped' icon and name
                        if item_data.all_name is not None:
                            pal_section['Tooltip'] = item_data.all_name

                        if item_data.all_icon is not None:
                            icon = item_data.all_icon
                        else:
                            icon = pal_section['Image']
                        # Bug in Portal 2 - palette icons must end with '.png',
                        # so force that to be the case for all icons.
                        if icon.casefold().endswith('.vtf'):
                            icon = icon[:-3] + 'png'
                        pal_section['Image'] = icon

                    pal_section['Position'] = "{x} {y} 0".format(
                        x=palette_items[index] % 4,
                        y=palette_items[index] // 4,
                    )
                else:
                    # This subtype isn't on the palette, delete the entire
                    # "Palette" block.
                    del editor_section[editor_sec_index]
                    break

        # Apply configured default values to this item
        prop_overrides = prop_conf.get(self.id, {})
        for prop_section in new_editor.find_all("Editor", "Properties"):
            for item_prop in prop_section:
                if item_prop.bool('BEE2_ignore'):
                    continue

                if item_prop.name.casefold() in prop_overrides:
                    item_prop['DefaultValue'] = prop_overrides[item_prop.name.casefold()]

        # OccupiedVoxels does not allow specifying 'volume' regions like
        # EmbeddedVoxel. Implement that.

        # First for 32^2 cube sections.
        for voxel_part in new_editor.find_all("Exporting", "OccupiedVoxels", "SurfaceVolume"):
            if 'subpos1' not in voxel_part or 'subpos2' not in voxel_part:
                LOGGER.warning(
                    'Item {} has invalid OccupiedVoxels part '
                    '(needs SubPos1 and SubPos2)!',
                    self.id
                )
                continue
            voxel_part.name = "Voxel"
            bbox_min, bbox_max = Vec.bbox(
                voxel_part.vec('subpos1'),
                voxel_part.vec('subpos2'),
            )
            del voxel_part['subpos1']
            del voxel_part['subpos2']
            for pos in Vec.iter_grid(bbox_min, bbox_max):
                voxel_part.append(Property(
                    "Surface", [
                        Property("Pos", str(pos)),
                    ])
                )

        # Full blocks
        for occu_voxels in new_editor.find_all("Exporting", "OccupiedVoxels"):
            for voxel_part in list(occu_voxels.find_all("Volume")):
                del occu_voxels['Volume']

                if 'pos1' not in voxel_part or 'pos2' not in voxel_part:
                    LOGGER.warning(
                        'Item {} has invalid OccupiedVoxels part '
                        '(needs Pos1 and Pos2)!',
                        self.id
                    )
                    continue
                voxel_part.name = "Voxel"
                bbox_min, bbox_max = Vec.bbox(
                    voxel_part.vec('pos1'),
                    voxel_part.vec('pos2'),
                )
                del voxel_part['pos1']
                del voxel_part['pos2']
                for pos in Vec.iter_grid(bbox_min, bbox_max):
                    new_part = voxel_part.copy()
                    new_part['Pos'] = str(pos)
                    occu_voxels.append(new_part)

        return (
            new_editor,
            item_data.editor_extra,
            # Add all_conf first so it's conditions run first by default
            self.all_conf + item_data.vbsp_config,
        )


class ItemConfig(PakObject, allow_mult=True, has_img=False):
    """Allows adding additional configuration for items.

    The ID should match an item ID.
    """
    def __init__(self, it_id, all_conf, version_conf):
        self.id = it_id
        self.versions = version_conf
        self.all_conf = all_conf

    @classmethod
    def parse(cls, data: ParseData):
        vers = {}

        all_config = get_config(
            data.info,
            data.zip_file,
            'items',
            pak_id=data.pak_id,
            prop_name='all_conf',
        )
        set_cond_source(all_config, '<ItemConfig {}:{} all_conf>'.format(
            data.pak_id, data.id,
        ))

        for ver in data.info.find_all('Version'):  # type: Property
            ver_id = ver['ID', 'VER_DEFAULT']
            vers[ver_id] = styles = {}
            for sty_block in ver.find_all('Styles'):
                for style in sty_block:  # type: Property
                    file_loc = 'items/' + style.value + '.cfg'
                    with data.zip_file.open(file_loc) as f:
                        styles[style.real_name] = conf = Property.parse(
                            f,
                            data.pak_id + ':' + file_loc,
                        )
                    set_cond_source(conf, "<ItemConfig {}:{} in '{}'>".format(
                        data.pak_id, data.id, style.real_name,
                    ))

        return cls(
            data.id,
            all_config,
            vers,
        )

    def add_over(self, override: 'ItemConfig'):
        self.all_conf += override.all_conf.copy()

        for vers_id, styles in override.versions.items():
            our_styles = self.versions.setdefault(vers_id, {})
            for sty_id, style in styles.items():
                if sty_id not in our_styles:
                    our_styles[sty_id] = style.copy()
                else:
                    our_styles[sty_id] += style.copy()

    @staticmethod
    def export(exp_data: ExportData):
        """This export is done in Item.export().

        Here we don't know the version set for each item.
        """
        pass


class QuotePack(PakObject):
    def __init__(
            self,
            quote_id,
            selitem_data: 'SelitemData',
            config: Property,
            chars=None,
            skin=None,
            studio: str=None,
            studio_actor='',
            cam_loc: Vec=None,
            turret_hate=False,
            interrupt=0.0,
            cam_pitch=0.0,
            cam_yaw=0.0,
            ):
        self.id = quote_id
        self.selitem_data = selitem_data
        self.cave_skin = skin
        self.config = config
        set_cond_source(config, 'QuotePack <{}>'.format(quote_id))
        self.chars = chars or ['??']
        self.studio = studio
        self.studio_actor = studio_actor
        self.cam_loc = cam_loc
        self.inter_chance = interrupt
        self.cam_pitch = cam_pitch
        self.cam_yaw = cam_yaw
        self.turret_hate = turret_hate

    @classmethod
    def parse(cls, data):
        """Parse a voice line definition."""
        selitem_data = get_selitem_data(data.info)
        chars = {
            char.strip()
            for char in
            data.info['characters', ''].split(',')
            if char.strip()
        }

        # For Cave Johnson voicelines, this indicates what skin to use on the
        # portrait.
        port_skin = srctools.conv_int(data.info['caveSkin', None], None)

        monitor_data = data.info.find_key('monitor', None)

        if monitor_data.value is not None:
            mon_studio = monitor_data['studio']
            mon_studio_actor = monitor_data['studio_actor', '']
            mon_interrupt = monitor_data.float('interrupt_chance', 0)
            mon_cam_loc = monitor_data.vec('Cam_loc')
            mon_cam_pitch, mon_cam_yaw, _ = monitor_data.vec('Cam_angles')
            turret_hate = monitor_data.bool('TurretShoot')
        else:
            mon_studio = mon_cam_loc = None
            mon_interrupt = mon_cam_pitch = mon_cam_yaw = 0
            mon_studio_actor = ''
            turret_hate = False

        config = get_config(
            data.info,
            data.zip_file,
            'voice',
            pak_id=data.pak_id,
            prop_name='file',
        )

        return cls(
            data.id,
            selitem_data,
            config,
            chars=chars,
            skin=port_skin,
            studio=mon_studio,
            studio_actor=mon_studio_actor,
            interrupt=mon_interrupt,
            cam_loc=mon_cam_loc,
            cam_pitch=mon_cam_pitch,
            cam_yaw=mon_cam_yaw,
            turret_hate=turret_hate,
            )

    def add_over(self, override: 'QuotePack'):
        """Add the additional lines to ourselves."""
        self.selitem_data = join_selitem_data(
            self.selitem_data,
            override.selitem_data
        )
        self.config += override.config
        self.config.merge_children(
            'quotes_sp',
            'quotes_coop',
        )
        if self.cave_skin is None:
            self.cave_skin = override.cave_skin

        if self.studio is None:
            self.studio = override.studio
            self.studio_actor = override.studio_actor
            self.cam_loc = override.cam_loc
            self.inter_chance = override.inter_chance
            self.cam_pitch = override.cam_pitch
            self.cam_yaw = override.cam_yaw
            self.turret_hate = override.turret_hate


    def __repr__(self):
        return '<Voice:' + self.id + '>'

    @staticmethod
    def export(exp_data: ExportData):
        """Export the quotepack."""
        if exp_data.selected is None:
            return  # No quote pack!

        try:
            voice = QuotePack.by_id(exp_data.selected)  # type: QuotePack
        except KeyError:
            raise Exception(
                "Selected voice ({}) doesn't exist?".format(exp_data.selected)
            ) from None

        vbsp_config = exp_data.vbsp_conf  # type: Property

        # We want to strip 'trans' sections from the voice pack, since
        # they're not useful.
        for prop in voice.config:
            if prop.name == 'quotes':
                vbsp_config.append(QuotePack.strip_quote_data(prop))
            else:
                vbsp_config.append(prop.copy())

        # Set values in vbsp_config, so flags can determine which voiceline
        # is selected.
        options = vbsp_config.ensure_exists('Options')

        options['voice_pack'] = voice.id
        options['voice_char'] = ','.join(voice.chars)

        if voice.cave_skin is not None:
            options['cave_port_skin'] = str(voice.cave_skin)

        if voice.studio is not None:
            options['voice_studio_inst'] = voice.studio
            options['voice_studio_actor'] = voice.studio_actor
            options['voice_studio_inter_chance'] = str(voice.inter_chance)
            options['voice_studio_cam_loc'] = voice.cam_loc.join(' ')
            options['voice_studio_cam_pitch'] = str(voice.cam_pitch)
            options['voice_studio_cam_yaw'] = str(voice.cam_yaw)
            options['voice_studio_should_shoot'] = srctools.bool_as_int(voice.turret_hate)

        # Copy the config files for this voiceline..
        for prefix, pretty in [
                ('', 'normal'),
                ('mid_', 'MidChamber'),
                ('resp_', 'Responses')]:
            path = os.path.join(
                os.getcwd(),
                '..',
                'config',
                'voice',
                prefix.upper() + voice.id + '.cfg',
            )
            LOGGER.info(path)
            if os.path.isfile(path):
                shutil.copy(
                    path,
                    exp_data.game.abs_path(
                        'bin/bee2/{}voice.cfg'.format(prefix)
                    )
                )
                LOGGER.info('Written "{}voice.cfg"', prefix)
            else:
                LOGGER.info('No {} voice config!', pretty)

    @staticmethod
    def strip_quote_data(prop: Property, _depth=0):
        """Strip unused property blocks from the config files.

        This removes data like the captions which the compiler doesn't need.
        The returned property tree is a deep-copy of the original.
        """
        children = []
        for sub_prop in prop:
            # Make sure it's in the right nesting depth - flags might
            # have arbitrary props in lower depths..
            if _depth == 3:  # 'Line' blocks
                if sub_prop.name == 'trans':
                    continue
                elif sub_prop.name == 'name' and 'id' in prop:
                    continue  # The name isn't needed if an ID is available
            elif _depth == 2 and sub_prop.name == 'name':
                # In the "quote" section, the name isn't used in the compiler.
                continue

            if sub_prop.has_children():
                children.append(QuotePack.strip_quote_data(sub_prop, _depth + 1))
            else:
                children.append(Property(sub_prop.real_name, sub_prop.value))
        return Property(prop.real_name, children)


class Skybox(PakObject):
    def __init__(
            self,
            sky_id,
            selitem_data: 'SelitemData',
            config: Property,
            fog_opts: Property,
            mat,
            ):
        self.id = sky_id
        self.selitem_data = selitem_data
        self.material = mat
        self.config = config
        set_cond_source(config, 'Skybox <{}>'.format(sky_id))
        self.fog_opts = fog_opts

        # Extract this for selector windows to easily display
        self.fog_color = Vec.from_str(
            fog_opts['primarycolor' ''],
            255, 255, 255
        )

    @classmethod
    def parse(cls, data: ParseData):
        """Parse a skybox definition."""
        selitem_data = get_selitem_data(data.info)
        mat = data.info['material', 'sky_black']
        config = get_config(
            data.info,
            data.zip_file,
            'skybox',
            pak_id=data.pak_id,
        )

        fog_opts = data.info.find_key("Fog", [])

        return cls(
            data.id,
            selitem_data,
            config,
            fog_opts,
            mat,
        )

    def add_over(self, override: 'Skybox'):
        """Add the additional vbsp_config commands to ourselves."""
        self.selitem_data = join_selitem_data(
            self.selitem_data,
            override.selitem_data
        )
        self.config += override.config
        self.fog_opts += override.fog_opts.copy()

    def __repr__(self):
        return '<Skybox ' + self.id + '>'

    @staticmethod
    def export(exp_data: ExportData):
        """Export the selected skybox."""
        if exp_data.selected is None:
            return  # No skybox..

        try:
            skybox = Skybox.by_id(exp_data.selected)  # type: Skybox
        except KeyError:
            raise Exception(
                "Selected skybox ({}) doesn't exist?".format(exp_data.selected)
            )

        exp_data.vbsp_conf.set_key(
            ('Textures', 'Special', 'Sky'),
            skybox.material,
        )

        exp_data.vbsp_conf.append(skybox.config.copy())

        # Styles or other items shouldn't be able to set fog settings..
        if 'fog' in exp_data.vbsp_conf:
            del exp_data.vbsp_conf['fog']

        fog_opts = skybox.fog_opts.copy()
        fog_opts.name = 'Fog'

        exp_data.vbsp_conf.append(fog_opts)


class Music(PakObject):

    def __init__(
            self,
            music_id,
            selitem_data: 'SelitemData',
            config: Property=None,
            inst=None,
            sound=None,
            sample=None,
            pack=(),
            loop_len=0,
            ):
        self.id = music_id
        self.config = config or Property(None, [])
        set_cond_source(config, 'Music <{}>'.format(music_id))
        self.inst = inst
        self.sound = sound
        self.packfiles = list(pack)
        self.len = loop_len
        self.sample = sample

        self.selitem_data = selitem_data

        # Set attributes on this so UI.load_packages() can easily check for
        # which are present...
        sound_channels = ('base', 'speedgel', 'bouncegel', 'tbeam',)
        if isinstance(sound, Property):
            for chan in sound_channels:
                setattr(self, 'has_' + chan, bool(sound[chan, '']))
            self.has_synced_tbeam = self.has_tbeam and sound.bool('sync_funnel')
        else:
            for chan in sound_channels:
                setattr(self, 'has_' + chan, False)
            self.has_synced_tbeam = False

    @classmethod
    def parse(cls, data: ParseData):
        """Parse a music definition."""
        selitem_data = get_selitem_data(data.info)
        inst = data.info['instance', None]
        sound = data.info.find_key('soundscript', '')  # type: Property

        # The sample music file to play, if found.
        rel_sample = data.info['sample', '']
        if rel_sample:
            sample = os.path.abspath('../sounds/music_samp/' + rel_sample)
            zip_sample = 'resources/music_samp/' + rel_sample
            try:
                with zip_open_bin(data.zip_file, zip_sample):
                    pass
            except KeyError:
                LOGGER.warning(
                    'Music sample for <{}> does not exist in zip: "{}"',
                    data.id,
                    zip_sample,
                )
        else:
            sample = None

        snd_length = data.info['loop_len', '0']
        if ':' in snd_length:
            # Allow specifying lengths as min:sec.
            minute, second = snd_length.split(':')
            snd_length = 60 * srctools.conv_int(minute) + srctools.conv_int(second)
        else:
            snd_length = srctools.conv_int(snd_length)

        if not sound.has_children():
            sound = sound.value

        packfiles = [
            prop.value
            for prop in
            data.info.find_all('pack')
        ]

        config = get_config(
            data.info,
            data.zip_file,
            'music',
            pak_id=data.pak_id,
        )
        return cls(
            data.id,
            selitem_data,
            inst=inst,
            sound=sound,
            sample=sample,
            config=config,
            pack=packfiles,
            loop_len=snd_length,
        )

    def add_over(self, override: 'Music'):
        """Add the additional vbsp_config commands to ourselves."""
        self.config.append(override.config)
        self.selitem_data = join_selitem_data(
            self.selitem_data,
            override.selitem_data
        )

    def __repr__(self):
        return '<Music ' + self.id + '>'

    @staticmethod
    def export(exp_data: ExportData):
        """Export the selected music."""
        if exp_data.selected is None:
            return  # No music..

        try:
            music = Music.by_id(exp_data.selected)  # type: Music
        except KeyError:
            raise Exception(
                "Selected music ({}) doesn't exist?".format(exp_data.selected)
            ) from None

        vbsp_config = exp_data.vbsp_conf

        if isinstance(music.sound, Property):
            # We want to generate the soundscript - copy over the configs.
            vbsp_config.append(Property('MusicScript', music.sound.value))
            script = 'music.BEE2'
        else:
            script = music.sound

        # Set the instance/ambient_generic file that should be used.
        if script is not None:
            vbsp_config.set_key(
                ('Options', 'music_SoundScript'),
                script,
            )
        if music.inst is not None:
            vbsp_config.set_key(
                ('Options', 'music_instance'),
                music.inst,
            )
        vbsp_config.set_key(
            ('Options', 'music_looplen'),
            str(music.len),
        )

        # If we need to pack, add the files to be unconditionally packed.
        if music.packfiles:
            vbsp_config.set_key(
                ('PackTriggers', 'Forced'),
                [
                    Property('File', file)
                    for file in
                    music.packfiles
                ],
            )

        # Allow flags to detect the music that's used
        vbsp_config.set_key(('Options', 'music_ID'), music.id)
        vbsp_config += music.config.copy()


class StyleVar(PakObject, allow_mult=True, has_img=False):
    def __init__(
            self,
            var_id,
            name,
            styles,
            unstyled=False,
            default=False,
            desc='',
            ):
        self.id = var_id
        self.name = name
        self.default = default
        self.enabled = default
        self.desc = desc
        if unstyled:
            self.styles = None
        else:
            self.styles = styles

    @classmethod
    def parse(cls, data: 'ParseData'):
        name = data.info['name', '']

        unstyled = srctools.conv_bool(data.info['unstyled', '0'])
        default = srctools.conv_bool(data.info['enabled', '0'])
        styles = [
            prop.value
            for prop in
            data.info.find_all('Style')
        ]
        desc = '\n'.join(
            prop.value
            for prop in
            data.info.find_all('description')
        )
        return cls(
            data.id,
            name,
            styles,
            unstyled=unstyled,
            default=default,
            desc=desc,
        )

    def add_over(self, override):
        """Override a stylevar to add more compatible styles."""
        # Setting it to be unstyled overrides any other values!
        if self.styles is None:
            return
        elif override.styles is None:
            self.styles = None
        else:
            self.styles.extend(override.styles)

        if not self.name:
            self.name = override.name

        # If they both have descriptions, add them together.
        # Don't do it if they're both identical though.
        # bool(strip()) = has a non-whitespace character
        stripped_over = override.desc.strip()
        if stripped_over and stripped_over not in self.desc:
            if self.desc.strip():
                self.desc += '\n\n' + override.desc
            else:
                self.desc = override.desc

    def __repr__(self):
        return '<Stylevar "{}", name="{}", default={}, styles={}>:\n{}'.format(
            self.id,
            self.name,
            self.default,
            ','.join(self.styles),
            self.desc,
        )

    def applies_to_style(self, style):
        """Check to see if this will apply for the given style.

        """
        if self.styles is None:
            return True  # Unstyled stylevar

        if style.id in self.styles:
            return True

        return any(
            base.id in self.styles
            for base in
            style.bases
        )

    def applies_to_all(self):
        """Check if this applies to all styles."""
        if self.styles is None:
            return True

        for style in Style.all():  # type: Style
            if not self.applies_to_style(style):
                return False
        return True

    @staticmethod
    def export(exp_data: ExportData):
        """Export style var selections into the config.

        The .selected attribute is a dict mapping ids to the boolean value.
        """
        # Add the StyleVars block, containing each style_var.

        exp_data.vbsp_conf.append(Property('StyleVars', [
            Property(key, srctools.bool_as_int(val))
            for key, val in
            exp_data.selected.items()
        ]))


class StyleVPK(PakObject, has_img=False):
    """A set of VPK files used for styles.

    These are copied into _dlc3, allowing changing the in-editor wall
    textures.
    """
    def __init__(self, vpk_id, file_count=0):
        """Initialise a StyleVPK object."""
        self.id = vpk_id

    @classmethod
    def parse(cls, data: ParseData):
        vpk_name = data.info['filename']
        dest_folder = os.path.join('../vpk_cache', data.id.casefold())

        os.makedirs(dest_folder, exist_ok=True)

        zip_file = data.zip_file  # type: ZipFile

        has_files = False
        source_folder = os.path.normpath('vpk/' + vpk_name)

        for filename in zip_names(zip_file):
            if os.path.normpath(filename).startswith(source_folder):
                dest_loc = os.path.join(
                    dest_folder,
                    os.path.relpath(filename, source_folder)
                )
                os.makedirs(os.path.dirname(dest_loc), exist_ok=True)
                with zip_open_bin(zip_file, filename) as fsrc:
                    with open(dest_loc, 'wb') as fdest:
                        shutil.copyfileobj(fsrc, fdest)
                has_files = True

        if not has_files:
            raise Exception(
                'VPK object "{}" has no associated files!'.format(data.id)
            )

        return cls(data.id)

    @staticmethod
    def export(exp_data: ExportData):
        sel_vpk = exp_data.selected_style.vpk_name  # type: Style

        if sel_vpk:
            for vpk in StyleVPK.all():  # type: StyleVPK
                if vpk.id.casefold() == sel_vpk:
                    sel_vpk = vpk
                    break
            else:
                sel_vpk = None
        else:
            sel_vpk = None

        try:
            dest_folder = StyleVPK.clear_vpk_files(exp_data.game)
        except PermissionError:
            return  # We can't edit the VPK files - P2 is open..

        if exp_data.game.steamID == utils.STEAM_IDS['PORTAL2']:
            # In Portal 2, we make a dlc3 folder - this changes priorities,
            # so the soundcache will be regenerated. Just copy the old one over.
            sound_cache = os.path.join(
                dest_folder, 'maps', 'soundcache', '_master.cache'
            )
            LOGGER.info('Sound cache: {}', sound_cache)
            if not os.path.isfile(sound_cache):
                LOGGER.info('Copying over soundcache file for DLC3..')
                os.makedirs(os.path.dirname(sound_cache), exist_ok=True)
                try:
                    shutil.copy(
                        exp_data.game.abs_path(
                            'portal2_dlc2/maps/soundcache/_master.cache',
                        ),
                        sound_cache,
                    )
                except FileNotFoundError:
                    # It's fine, this will be regenerated automatically
                    pass

        # Generate the VPK.
        vpk_file = VPK(os.path.join(dest_folder, 'pak01_dir.vpk'), mode='w')
        if sel_vpk is not None:
            src_folder = os.path.abspath(
                os.path.join(
                    '../vpk_cache',
                    sel_vpk.id.casefold()
                ))
            vpk_file.add_folder(src_folder)

        # Additionally, pack in game/vpk_override/ into the vpk - this allows
        # users to easily override resources in general.

        override_folder = exp_data.game.abs_path('vpk_override')
        os.makedirs(override_folder, exist_ok=True)

        # Also write a file to explain what it's for..
        with open(os.path.join(override_folder, 'BEE2_README.txt'), 'w') as f:
            f.write(VPK_OVERRIDE_README)

        vpk_file.add_folder(override_folder)
        del vpk_file['BEE2_README.txt']  # Don't add this to the VPK though..

        # Fix Valve's fail with the cubemap file - if we have the resource,
        # override the original via DLC3.
        try:
            cave_cubemap_file = open(
                '../cache/resources/materials/BEE2/cubemap_cave01.vtf',
                'rb'
            )
        except FileNotFoundError:
            pass
        else:
            with cave_cubemap_file:
                try:
                    vpk_file.add_file(
                        ('materials/cubemaps/', 'cubemap_cave01', 'vtf'),
                        cave_cubemap_file.read(),
                    )
                except FileExistsError:
                    # The user might have added it to the vpk_override/ folder.
                    pass

        vpk_file.write_dirfile()

        LOGGER.info('Written {} files to VPK!', len(vpk_file))


    @staticmethod
    def iter_vpk_names():
        """Iterate over VPK filename suffixes.

        The first is '_dir.vpk', then '_000.vpk' with increasing
        numbers.
        """
        yield '_dir.vpk'
        for i in range(999):
            yield '_{:03}.vpk'.format(i)

    @staticmethod
    def clear_vpk_files(game) -> str:
        """Remove existing VPKs files from a game.

         We want to leave other files - otherwise users will end up
         regenerating the sound cache every time they export.

        This returns the path to the game folder.
        """
        dest_folder = game.abs_path(VPK_FOLDER.get(
            game.steamID,
            'portal2_dlc3',
        ))

        os.makedirs(dest_folder, exist_ok=True)
        try:
            for file in os.listdir(dest_folder):
                if file[:6] == 'pak01_':
                    os.remove(os.path.join(dest_folder, file))
        except PermissionError:
            # The player might have Portal 2 open. Abort changing the VPK.
            LOGGER.warning("Couldn't replace VPK files. Is Portal 2 "
                           "or Hammer open?")
            raise

        return dest_folder


class Elevator(PakObject):
    """An elevator video definition.

    This is mainly defined just for Valve's items - you can't pack BIKs.
    """
    def __init__(
            self,
            elev_id,
            selitem_data: 'SelitemData',
            video,
            vert_video=None,
            ):
        self.id = elev_id

        self.selitem_data = selitem_data

        if vert_video is None:
            self.has_orient = False
            self.horiz_video = video
            self.vert_video = video
        else:
            self.has_orient = True
            self.horiz_video = video
            self.vert_video = vert_video

    @classmethod
    def parse(cls, data):
        info = data.info
        selitem_data = get_selitem_data(info)

        if 'vert_video' in info:
            video = info['horiz_video']
            vert_video = info['vert_video']
        else:
            video = info['video']
            vert_video = None

        return cls(
            data.id,
            selitem_data,
            video,
            vert_video,
        )

    def __repr__(self):
        return '<Elevator ' + self.id + '>'

    @staticmethod
    def export(exp_data: ExportData):
        """Export the chosen video into the configs."""
        style = exp_data.selected_style  # type: Style
        vbsp_config = exp_data.vbsp_conf  # type: Property

        if exp_data.selected is None:
            elevator = None
        else:
            try:
                elevator = Elevator.by_id(exp_data.selected)  # type: Elevator
            except KeyError:
                raise Exception(
                    "Selected elevator ({}) "
                    "doesn't exist?".format(exp_data.selected)
                ) from None

        if style.has_video:
            if elevator is None:
                # Use a randomised video
                vbsp_config.set_key(
                    ('Elevator', 'type'),
                    'RAND',
                )
            elif elevator.id == 'VALVE_BLUESCREEN':
                # This video gets a special script and handling
                vbsp_config.set_key(
                    ('Elevator', 'type'),
                    'BSOD',
                )
            else:
                # Use the particular selected video
                vbsp_config.set_key(
                    ('Elevator', 'type'),
                    'FORCE',
                )
                vbsp_config.set_key(
                    ('Elevator', 'horiz'),
                    elevator.horiz_video,
                )
                vbsp_config.set_key(
                    ('Elevator', 'vert'),
                    elevator.vert_video,
                )
        else:  # No elevator video for this style
            vbsp_config.set_key(
                ('Elevator', 'type'),
                'NONE',
            )


class PackList(PakObject, allow_mult=True, has_img=False):
    def __init__(self, pak_id, files, mats):
        self.id = pak_id
        self.files = files
        self.trigger_mats = mats

    @classmethod
    def parse(cls, data):
        conf = data.info.find_key('Config', '')
        mats = [
            prop.value
            for prop in
            data.info.find_all('AddIfMat')
        ]

        files = []

        if conf.has_children():
            # Allow having a child block to define packlists inline
            files = [
                prop.value
                for prop in conf
            ]
        elif conf.value:
            path = 'pack/' + conf.value + '.cfg'
            try:
                with data.zip_file.open(path) as f:
                    # Each line is a file to pack.
                    # Skip blank lines, strip whitespace, and
                    # allow // comments.
                    for line in f:
                        line = srctools.clean_line(line)
                        if line:
                            files.append(line)
            except KeyError as ex:
                raise FileNotFoundError(
                    '"{}:{}" not in zip!'.format(
                        data.id,
                        path,
                    )
                ) from ex

        # We know that if it's a material, it must be packing the VMT at the
        # very least.
        for mat in mats:
            files.append('materials/' + mat + '.vmt')

        if not files:
            raise ValueError('"{}" has no files to pack!'.format(data.id))

        if CHECK_PACKFILE_CORRECTNESS:
            # Use normpath so sep differences are ignored, plus case.
            zip_files = {
                os.path.normpath(file).casefold()
                for file in
                zip_names(data.zip_file)
                if file.startswith('resources')
            }
            for file in files:
                if file.startswith(('-#', 'precache_sound:')):
                    # Used to disable stock soundscripts, and precache sounds
                    # Not to pack - ignore.
                    continue

                file = file.lstrip('#')  # This means to put in soundscript too...

                #  Check to make sure the files exist...
                file = os.path.join('resources', os.path.normpath(file)).casefold()
                if file not in zip_files:
                    LOGGER.warning('Warning: "{file}" not in zip! ({pak_id})',
                        file=file,
                        pak_id=data.pak_id,
                    )

        return cls(
            data.id,
            files,
            mats,
        )

    def add_over(self, override):
        """Override items just append to the list of files."""
        # Dont copy over if it's already present
        for item in override.files:
            if item not in self.files:
                self.files.append(item)

        for item in override.trigger_mats:
            if item not in self.trigger_mats:
                self.trigger_mats.append(item)

    @staticmethod
    def export(exp_data: ExportData):
        """Export all the packlists."""

        pack_block = Property('PackList', [])

        # A list of materials which will casue a specific packlist to be used.
        pack_triggers = Property('PackTriggers', [])

        for pack in PackList.all():  # type: PackList
            # Build a
            # "Pack_id"
            # {
            # "File" "filename"
            # "File" "filename"
            # }
            # block for each packlist
            files = [
                Property('File', file)
                for file in
                pack.files
            ]
            pack_block.append(Property(
                pack.id,
                files,
            ))

            for trigger_mat in pack.trigger_mats:
                pack_triggers.append(
                    Property('Material', [
                        Property('Texture', trigger_mat),
                        Property('PackList', pack.id),
                    ])
                )

        # Only add packtriggers if there's actually a value
        if pack_triggers.value:
            exp_data.vbsp_conf.append(pack_triggers)

        LOGGER.info('Writing packing list!')
        with open(exp_data.game.abs_path('bin/bee2/pack_list.cfg'), 'w') as pack_file:
            for line in pack_block.export():
                pack_file.write(line)


class EditorSound(PakObject, has_img=False):
    """Add sounds that are usable in the editor.

    The editor only reads in game_sounds_editor, so custom sounds must be
    added here.
    The ID is the name of the sound, prefixed with 'BEE2_Editor.'.
    The values in 'keys' will form the soundscript body.
    """
    def __init__(self, snd_name, data):
        self.id = 'BEE2_Editor.' + snd_name
        self.data = data
        data.name = self.id

    @classmethod
    def parse(cls, data):
        return cls(
            snd_name=data.id,
            data=data.info.find_key('keys', [])
        )

    @staticmethod
    def export(exp_data: ExportData):
        """Export EditorSound objects."""
        # Just command the game to do the writing.
        exp_data.game.add_editor_sounds(
            EditorSound.all()
        )


class BrushTemplate(PakObject, has_img=False):
    """A template brush which will be copied into the map, then retextured.

    This allows the sides of the brush to swap between wall/floor textures
    based on orientation.
    All world and detail brushes from the given VMF will be copied.
    """
    def __init__(self, temp_id, vmf_file: VMF, force=None, keep_brushes=True):
        """Import in a BrushTemplate object.

        This copies the solids out of VMF_FILE and into TEMPLATE_FILE.
        If force is set to 'world' or 'detail', the other type will be converted.
        If keep_brushes is false brushes will be skipped (for TemplateOverlay).
        """
        self.id = temp_id
        # We don't actually store the solids here - put them in
        # the TEMPLATE_FILE VMF. That way the original VMF object can vanish.

        self.temp_world = {}
        self.temp_detail = {}

        visgroup_names = {
            vis.id: vis.name
            for vis in
            vmf_file.vis_tree
        }

        # For each template, give them a visgroup to match - that
        # makes it easier to swap between them.
        temp_visgroup_id = TEMPLATE_FILE.create_visgroup(temp_id).id

        if force.casefold() == 'detail':
            force_is_detail = True
        elif force.casefold() == 'world':
            force_is_detail = False
        else:
            force_is_detail = None

        # Parse through a config entity in the template file.
        conf_ents = list(vmf_file.by_class['bee2_template_conf'])
        if len(conf_ents) > 1:
            raise ValueError(
                'Template "{}" has multiple configuration entities!'.format(temp_id)
            )
        elif len(conf_ents) == 1:
            config = conf_ents[0]
            conf_auto_visgroup = int(srctools.conv_bool(config['detail_auto_visgroup']))
            if srctools.conv_bool(config['discard_brushes']):
                keep_brushes = False
            is_scaling = srctools.conv_bool(config['is_scaling'])
            if config['temp_type'] == 'detail':
                force_is_detail = True
            elif config['temp_type'] == 'world':
                force_is_detail = False
        else:
            conf_auto_visgroup = is_scaling = False

        if is_scaling:
            raise NotImplementedError()  # TODO
        elif keep_brushes:
            for brushes, is_detail, vis_ids in self.yield_world_detail(vmf_file):
                if force_is_detail is not None:
                    export_detail = force_is_detail
                else:
                    export_detail = is_detail
                if len(vis_ids) > 1:
                    raise ValueError('Template "{}" has brush with two'
                                     ' visgroups!'.format(
                        temp_id
                    ))
                visgroups = [
                    visgroup_names[id]
                    for id in
                    vis_ids
                ]
                # No visgroup = ''
                visgroup = visgroups[0] if visgroups else ''

                # Auto-visgroup puts func_detail ents in unique visgroups.
                if is_detail and not visgroup and conf_auto_visgroup:
                    visgroup = '__auto_group_{}__'.format(conf_auto_visgroup)
                    # Reuse as the unique index, >0 are True too..
                    conf_auto_visgroup += 1

                targ_dict = self.temp_detail if export_detail else self.temp_world
                try:
                    ent = targ_dict[temp_id, visgroup, export_detail]
                except KeyError:
                    ent = targ_dict[temp_id, visgroup, export_detail] = TEMPLATE_FILE.create_ent(
                        classname=(
                            'bee2_template_detail' if
                            export_detail
                            else 'bee2_template_world'
                        ),
                        template_id=temp_id,
                        visgroup=visgroup,
                    )
                ent.visgroup_ids.add(temp_visgroup_id)
                for brush in brushes:
                    ent.solids.append(
                        brush.copy(map=TEMPLATE_FILE, keep_vis=False)
                    )

        self.temp_overlays = []

        for overlay in vmf_file.by_class['info_overlay']:  # type: Entity
            visgroups = [
                visgroup_names[id]
                for id in
                overlay.visgroup_ids
                ]
            if len(visgroups) > 1:
                raise ValueError('Template "{}" has overlay with two'
                                 ' visgroups!'.format(
                    self.id,
                ))
            new_overlay = overlay.copy(
                map=TEMPLATE_FILE,
                keep_vis=False
            )
            new_overlay.visgroup_ids.add(temp_visgroup_id)
            new_overlay['template_id'] = self.id
            new_overlay['visgroup'] = visgroups[0] if visgroups else ''
            new_overlay['classname'] = 'bee2_template_overlay'
            TEMPLATE_FILE.add_ent(new_overlay)

            self.temp_overlays.append(new_overlay)

        if self.temp_detail is None and self.temp_world is None:
            if not self.temp_overlays and not is_scaling:
                LOGGER.warning('BrushTemplate "{}" has no data!', temp_id)

    @classmethod
    def parse(cls, data: ParseData):
        file = get_config(
            prop_block=data.info,
            zip_file=data.zip_file,
            folder='templates',
            pak_id=data.pak_id,
            prop_name='file',
            extension='.vmf',
        )
        file = VMF.parse(file)
        return cls(
            data.id,
            file,
            force=data.info['force', ''],
            keep_brushes=srctools.conv_bool(data.info['keep_brushes', '1'], True),
        )

    @staticmethod
    def export(exp_data: ExportData):
        """Write the template VMF file."""
        # Sort the visgroup list by name, to make it easier to search through.
        TEMPLATE_FILE.vis_tree.sort(key=lambda vis: vis.name)

        path = exp_data.game.abs_path('bin/bee2/templates.vmf')
        with open(path, 'w') as temp_file:
            TEMPLATE_FILE.export(temp_file, inc_version=False)

    @staticmethod
    def yield_world_detail(map: VMF) -> Iterator[Tuple[List[Solid], bool, set]]:
        """Yield all world/detail solids in the map.

        This also indicates if it's a func_detail, and the visgroup IDs.
        (Those are stored in the ent for detail, and the solid for world.)
        """
        for brush in map.brushes:
            yield [brush], False, brush.visgroup_ids
        for ent in map.by_class['func_detail']:
            yield ent.solids.copy(), True, ent.visgroup_ids


def desc_parse(info, id=''):
    """Parse the description blocks, to create data which matches richTextBox.

    """
    has_warning = False
    lines = []
    for prop in info.find_all("description"):
        if prop.has_children():
            for line in prop:
                if line.name and not has_warning:
                    LOGGER.warning('Old desc format: {}', id)
                    has_warning = True
                lines.append(line.value)
        else:
            lines.append(prop.value)

    return tkMarkdown.convert('\n'.join(lines))



def get_selitem_data(info):
    """Return the common data for all item types - name, author, description.
    """
    auth = sep_values(info['authors', ''])
    short_name = info['shortName', None]
    name = info['name']
    icon = info['icon', None]
    large_icon = info['iconlarge', None]
    group = info['group', '']
    sort_key = info['sort_key', '']
    desc = desc_parse(info, id=info['id'])
    if not group:
        group = None
    if not short_name:
        short_name = name

    return SelitemData(
        name,
        short_name,
        auth,
        icon,
        large_icon,
        desc,
        group,
        sort_key,
    )


def join_selitem_data(our_data: 'SelitemData', over_data: 'SelitemData'):
    """Join together two sets of selitem data.

    This uses the over_data values if defined, using our_data if not.
    Authors and descriptions will be joined to each other.
    """
    (
        our_name,
        our_short_name,
        our_auth,
        our_icon,
        our_large_icon,
        our_desc,
        our_group,
        our_sort_key,
    ) = our_data

    (
        over_name,
        over_short_name,
        over_auth,
        over_icon,
        over_large_icon,
        over_desc,
        over_group,
        over_sort_key,
    ) = over_data

    return SelitemData(
        our_name,
        our_short_name,
        our_auth + over_auth,
        over_icon if our_icon is None else our_icon,
        over_large_icon if our_large_icon is None else our_large_icon,
        tkMarkdown.join(our_desc, over_desc),
        over_group or our_group,
        over_sort_key or our_sort_key,
    )


def sep_values(string, delimiters=',;/'):
    """Split a string by a delimiter, and then strip whitespace.

    Multiple delimiter characters can be passed.
    """
    delim, *extra_del = delimiters
    if string == '':
        return []

    for extra in extra_del:
        string = string.replace(extra, delim)

    vals = string.split(delim)
    return [
        stripped for stripped in
        (val.strip() for val in vals)
        if stripped
    ]

if __name__ == '__main__':
    load_packages('packages//', False)
