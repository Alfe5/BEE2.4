import os
import os.path
import sys
import subprocess
import shutil
import random

from property_parser import Property
import utils

def log(text):
    print(text, flush=True)

def run_vrad(args):
    "Execute the original VRAD."
    args = [('"' + x + '"' if " " in x else x) for x in args]
    arg = os.path.join(os.getcwd(),"vrad_original") + " " + " ".join(args)
    log("Calling original VRAD...")
    log(arg)
    subprocess.call(arg)
    log("Done!")   

# MAIN
to_pack = [] # the file path for any items that we should be packing
to_pack_inst = {} # items to pack for a specific instance
to_pack_mat = {} # files to pack if material is used (by VBSP_styles only)

root = os.path.dirname(os.getcwd())
args = " ".join(sys.argv)
new_args=sys.argv[1:]
path=""
print(sys.argv)
game_dir = ""
next_is_game = False
for a in list(new_args):
    if next_is_game:
        next_is_game = False
        game_dir = a
    elif "sdk_content\\maps\\" in os.path.normpath(a):
        path=a
    elif a == "-game":
        next_is_game = True
    elif a.casefold() in ("-both", "-final", "-staticproplighting", "-staticproppolys", "-textureshadows"):
        new_args.remove(a)

new_args = ['-bounce', '2', '-noextra'] + new_args

# Fast args: -bounce 2 -noextra -game $gamedir $path\$file
# Final args: -both -final -staticproplighting -StaticPropPolys -textureshadows  -game $gamedir $path\$file

log("Map path is " + path)
if path == "":
    raise Exception("No map passed!")
    
if not path.endswith(".vmf"):
    path += ".vmf"

if os.path.basename(path) == "preview.vmf": # Is this a PeTI map?
    log("PeTI map detected! (is named preview.vmf")
    run_vrad(new_args)
else:
    log("Hammer map detected! Not forcing cheap lighting..")
    run_vrad(sys.argv[1:])
    
name = path[:-4]
pack_file = name + '.filelist.txt'

if os.path.isfile(pack_file):
    log("Pack list found, packing files!")
    arg_bits = [os.path.join(os.getcwd(),"bspzip"),
            "-addlist",
            '"' + name + '.bsp"',
            '"' + pack_file + '"',
            '"' + name + '.bsp"',
            "-game",
            '"' +game_dir + '"',
          ]
    arg = " ".join(arg_bits)
    log(arg)
    subprocess.call(arg)
    log("Packing complete!")