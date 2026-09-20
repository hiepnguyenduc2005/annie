"""Furnished two-storey home with real collision geometry and a stairwell.

The authored map supports ground-floor planning. Stair geometry is not a
claim that the currently selected locomotion policy can traverse it.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from robot.simulation.scenes import box, element, geom, make_scene, numbers

WALL = (.86, .85, .79, 1)
WOOD = (.42, .26, .15, 1)
CREAM = (.95, .91, .82, 1)
TEAL = (.20, .40, .39, 1)


def make_house(assets: Path, seed=2026):
    xml, record = make_scene(assets, 'empty', 0, seed)
    root = ET.fromstring(xml)
    root.set('model', 'annie_grandmas_house')
    world = root.find('worldbody')
    # Reuse the cached textured furniture meshes and all referenced assets.
    visuals = {name: copy.deepcopy(world.find(f"body[@name='env_{name}_visual']"))
               for name in ('chair', 'table')}
    for node in list(world):
        if node.get('name', '').startswith('env_'):
            world.remove(node)
    root.find('statistic').set('extent', '18')
    root.find('statistic').set('center', '0 0 1.5')
    for x in (-4, 4):
        element(world, 'light', name=f'env_daylight_{x}', pos=(x, 0, 9),
                dir=(0, 0, -1), directional='true', diffuse=(.65, .65, .62))
    geom(world, 'floor', 'plane', (0, 0, 0), (7, 6, .1), (.68, .58, .44, 1))
    floor = world.find("geom[@name='env_floor']")
    if root.find("asset/material[@name='env_wood_floor']") is not None:
        floor.set('material', 'env_wood_floor')
        floor.set('rgba', '1 1 1 1')

    def cube(name, pos, size, color=WOOD, upstairs=False):
        result = box(world, name, pos, size, color)
        if upstairs:
            result.set('group', '4')
        return result

    # Physical upper floor leaves a 1.7 m wide opening above the stair run.
    cube('upper_floor_main', (-1.1, 0, 2.91), (5.9, 6, .09), CREAM, True)
    cube('upper_floor_south', (5.9, -3, 2.91), (1.1, 3, .09), CREAM, True)
    cube('upper_landing', (5.9, 5.7, 2.91), (1.1, .3, .09), CREAM, True)
    rooms = []
    layouts = [
        ('living-room', 'Living room', -7, -6, 6, 4),
        ('study', 'Study', -7, -2, 6, 4),
        ('bedroom', 'Grandma’s bedroom', -7, 2, 6, 4),
        ('kitchen', 'Kitchen & dining', 1, -6, 6, 4),
        ('bathroom', 'Bathroom & utility', 1, -2, 3.6, 4),
        ('stair-hall', 'Stair hall', 4.6, -2, 2.4, 8),
        ('garden-room', 'Garden room', 1, 2, 3.6, 4),
        ('hallway', 'Entrance hall', -1, -6, 2, 12),
    ]
    for floor_no, z in ((0, 0), (1, 3)):
        upper = floor_no == 1
        prefix = f'f{floor_no}_'
        for suffix, pos, size in (
            ('west', (-7, 0, z+1.3), (.07, 6, 1.3)),
            ('east', (7, 0, z+1.3), (.07, 6, 1.3)),
            ('north', (0, 6, z+1.3), (7, .07, 1.3)),
            ('south_left', (-4, -6, z+1.3), (3, .07, 1.3)),
            ('south_right', (4, -6, z+1.3), (3, .07, 1.3)),
        ):
            cube(prefix+suffix, pos, size, WALL, upper)
        # 1.4 m open doorways connect every room to the centre hall.
        for x in (-1, 1):
            for y in (-6, -2, 2):
                for offset in (.65, 3.35):
                    cube(prefix+f'hall_{x}_{y}_{offset}', (x,y+offset,z+1.3),
                         (.06,.65,1.3), WALL, upper)
                cube(prefix+f'lintel_{x}_{y}', (x,y+2,z+2.35),
                     (.06,.7,.25), WALL, upper)
        for y in (-2, 2):
            cube(prefix+f'partition_left_{y}', (-4,y,z+1.3), (3,.06,1.3), WALL, upper)
            cube(prefix+f'partition_right_{y}', (2.8,y,z+1.3), (1.8,.06,1.3), WALL, upper)
        for idx, x in enumerate((-5, -2.6, 2.6, 5)):
            cube(prefix+f'window_{idx}', (x,5.91,z+1.65), (.75,.025,.55), (.45,.67,.75,1), upper)
            cube(prefix+f'window_frame_{idx}', (x,5.87,z+1.65), (.025,.035,.56), CREAM, upper)
        for key, label, x, y, width, depth in layouts:
            name = key if not upper else 'upstairs-' + key
            rooms.append(dict(id=name, label=label if not upper else 'Upstairs '+label.lower(),
                              x=x, y=y, width=width, height=depth, floor=floor_no, z=z))
        # Bedroom, bedding, bedside furniture.
        cube(prefix+'bed', (-5.0,4.1,z+.25), (.8,1.1,.25))
        cube(prefix+'mattress', (-5,4.1,z+.58), (.79,1.08,.08), CREAM, upper)
        cube(prefix+'blanket', (-5,3.85,z+.68), (.8,.8,.025), TEAL, upper)
        cube(prefix+'pillow', (-5,4.87,z+.72), (.55,.22,.08), CREAM, upper)
        cube(prefix+'headboard', (-5,5.24,z+.65), (.84,.06,.65), WOOD, upper)
        cube(prefix+'nightstand', (-3.75,4.8,z+.30), (.3,.3,.3), WOOD, upper)
        # Sitting area with a sofa, rug and television.
        cube(prefix+'sofa_seat', (-5.4,-4.6,z+.42), (1.1,.45,.18), TEAL, upper)
        cube(prefix+'sofa_back', (-5.4,-5,z+.84), (1.15,.12,.42), TEAL, upper)
        for x in (-6.5,-4.3):
            cube(prefix+f'sofa_arm_{x}', (x,-4.6,z+.66), (.12,.5,.32), TEAL, upper)
        cube(prefix+'rug', (-4.3,-3.6,z+.005), (1.6,.85,.005), (.55,.32,.23,1), upper)
        cube(prefix+'tv_cabinet', (-6.5,-2.7,z+.3), (.3,.6,.3), WOOD, upper)
        cube(prefix+'tv', (-6.48,-2.7,z+1.05), (.04,.58,.36), (.055,.065,.08,1), upper)
        # Study/work surface, bookshelf with individual books.
        cube(prefix+'desk', (-5.8,.9,z+.73), (.8,.45,.05), WOOD, upper)
        for x in (-6.4,-5.2):
            cube(prefix+f'desk_leg_{x}', (x,.9,z+.35), (.05,.3,.35), WOOD, upper)
        cube(prefix+'shelf', (-6.7,-.7,z+.8), (.2,.65,.8), WOOD, upper)
        for i in range(7):
            cube(prefix+f'book_{i}', (-6.46,-1.2+i*.16,z+1.23), (.025,.055,.18),
                 (.2+i*.08,.36,.42,1), upper)
        # Kitchen appliances, counter, sink and cooker.
        for key, x, y, sz, color in (
            ('counter',4.3,-5.55,(1.55,.4,.45),CREAM),
            ('fridge',6.3,-4.7,(.42,.48,.95),(.80,.84,.85,1)),
            ('sink',3.65,-5.55,(.3,.26,.025),(.3,.38,.4,1)),
            ('cooker',5,-5.5,(.37,.3,.025),(.12,.14,.15,1)),
        ):
            height = .92 if key in ('sink','cooker') else sz[2]
            cube(prefix+key,(x,y,z+height),sz,color,upper)
        # Bathroom fixtures, laundry and a garden table.
        cube(prefix+'bath', (3.5,.9,z+.3), (.85,.45,.3), CREAM, upper)
        cube(prefix+'bath_inset', (3.5,.9,z+.605), (.67,.3,.015), (.49,.68,.72,1), upper)
        cube(prefix+'toilet_base', (2,-1,z+.2), (.23,.3,.2), CREAM, upper)
        cube(prefix+'toilet_tank', (2,-.68,z+.55), (.23,.12,.35), CREAM, upper)
        cube(prefix+'washer', (4,-1.2,z+.42), (.38,.38,.42), CREAM, upper)
        cube(prefix+'garden_table', (3.2,4.9,z+.6), (.55,.55,.06), WOOD, upper)
        cube(prefix+'garden_table_base', (3.2,4.9,z+.27), (.1,.1,.27), WOOD, upper)
    # Correct group on every upper-floor furnishing, including the bed frame.
    for node in world.findall('geom'):
        if node.get('name','').startswith('env_f1_'):
            node.set('group','4')
    # Reuse the existing textured furniture once at accessible routine positions.
    for name, pos in (('chair',(-3,-3.3,0)), ('table',(2.9,-3.7,0))):
        if visuals[name] is not None:
            visuals[name].set('pos',numbers(pos))
            world.append(visuals[name])
        if name == 'chair':
            cube('chair_seat', (-3,-3.3,.48), (.36,.35,.07), TEAL)
            cube('chair_back', (-3,-2.98,.87), (.36,.06,.35), TEAL)
        else:
            cube('table_top', (2.9,-3.7,.75), (.55,.4,.045))
            for i,(dx,dy) in enumerate(((-.45,-.3),(.45,-.3),(-.45,.3),(.45,.3))):
                cube(f'table_leg_{i}',(2.9+dx,-3.7+dy,.35),(.04,.04,.35))
        if visuals[name] is not None:
            for node in world.findall('geom'):
                if node.get('name','').startswith('env_'+name+'_'):
                    node.set('group','3')
    # Authored visual prop, fixed beside the chair arm. No collision proxy or
    # navigation obstacle is added. Dimensions are metres (76 × 10 × 158 mm).
    phone_position = ((-2.86, -3.29, .535) if visuals['chair'] is not None
                      else (-2.86, -3.075, .63))
    phone = element(world, 'body', name='env_phone', pos=phone_position,
                    quat=(.994522, -.104528, 0, 0))
    for name, pos, size, color in (
        ('case', (0,0,0), (.038,.005,.079), (.035,.038,.044,1)),
        ('rim', (0,-.0051,0), (.035,.001,.076), (.20,.22,.25,1)),
        ('screen', (0,-.0062,0), (.032,.0005,.070), (.035,.07,.10,1)),
        ('speaker', (0,-.0068,.064), (.008,.0003,.0015), (.008,.009,.012,1)),
        ('home_bar', (0,-.0068,-.062), (.009,.0003,.001), (.55,.59,.62,1)),
        ('button', (.039,0,.025), (.001,.003,.012), (.15,.17,.19,1)),
    ):
        geom(phone, 'phone_'+name, 'box', pos, size, color,
             contype=0, conaffinity=0, group=2)
    # Optional setup view for a real rendered historical observation. Selecting
    # this camera does not generate a caption, inference, or memory record.
    element(world, 'camera', name='phone_memory', mode='targetbody',
            target='env_phone', pos=(-3.3,-4.35,1.1), fovy=38)
    # 18 real 16.67 cm risers; the upper landing is physically connected.
    for i in range(18):
        height = (i+1)/6
        cube(f'stair_{i:02d}', (5.7,.15+i*.3,height/2), (.6,.15,height/2), (.6,.48,.34,1))
    for side in (5.02,6.38):
        for i in range(0,18,3):
            height = (i+1)/6
            cube(f'rail_post_{side}_{i}', (side,.15+i*.3,height+.45), (.025,.025,.45), WOOD)
        geom(world, f'handrail_{side}', 'capsule', (0,0,0), (.035,), WOOD,
             fromto=(side,.15,1.07,side,5.25,3.9))
    qpos = [float(v) for v in root.find('keyframe/key').get('qpos').split()]
    qpos[:3] = [0,-4.6,.27]
    root.find('keyframe/key').set('qpos',numbers(qpos))
    root.find("worldbody/body[@name='base']").set('pos','0 -4.6 .27')
    from robot.simulation.daily_life import add_resident
    add_resident(world)
    record.update(id='grandmas-house', file='grandmas-house.xml', title='Grandma’s house · two floors',
                  description='Furnished 14 × 12 m home with eight ground-floor zones, an upper floor and physical stairs. Ground-floor navigation; stair traversal unqualified.',
                  category='house', rooms=rooms, floors=[{'id':0,'z':0},{'id':1,'z':3}],
                  stairs={'risers':18,'rise_m':1/6,'tread_m':.3,'width_m':1.2,'traversal_verified':False},
                  waypoints=[{'id':name,'x':x,'y':y,'floor':0} for name,x,y in (
                      ('home',0,-4.6),('living-room',-2,-4.2),('kitchen',1.9,-4.3),
                      ('hallway',0,0),('study',-2.3,0),('bedroom',-2.3,4),('garden-room',2.3,3.6))],
                  overview={'lookat':[0,0,1.2],'distance':20,'azimuth':120,'elevation':-55},
                  timeline=[],scenario_duration_s=300,daily_life=True)
    record['ground_truth'].update(room_size_m=[14,12,6], robot_home_position_m=qpos[:3],
         resident_present=True,posture='sitting',resident_origin_m=[-3,-3.65,0],
         support_surface='chair',temporal_scope='initial pose; current actor state is in /state.resident',
         objects={'bed':[-5,4.1,.58],'chair':[-3,-3.3,.48],'table':[2.9,-3.7,.75],'lamp':[-3.75,4.8,.68],
                  'phone':list(phone_position)})
    return ET.tostring(root,encoding='unicode'),record


def write_house(assets, output):
    output.mkdir(parents=True,exist_ok=True)
    xml,record=make_house(assets)
    (output/record['file']).write_text(xml)
    manifest_path=output/'manifest.json'
    manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {'version':3,'scenes':[]}
    manifest['scenes']=[record]+[s for s in manifest['scenes'] if s['id']!=record['id']]
    manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
    return record


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets',type=Path,default=Path('.cache/menagerie/unitree_go2'))
    parser.add_argument('--output',type=Path,default=Path('.data/simulation/scenes'))
    args=parser.parse_args()
    print(json.dumps(write_house(args.assets,args.output),indent=2))
