"""Shortslab's physical set. Run with Blender --background --python this_file.

All visible furniture and equipment is modeled here. No photographic cutouts.
Outputs editable .blend, runtime glTF, and an optional Cycles reference render.
"""
import bpy
import math
import random
import sys
from pathlib import Path
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'static/showroom3d'
OUT.mkdir(parents=True, exist_ok=True)
random.seed(1984)
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)

def material(name, color, rough=.5, metal=0, emission=0):
    m = bpy.data.materials.new(name)
    m.diffuse_color = (*color, 1)
    m.use_nodes = True
    bs = m.node_tree.nodes.get('Principled BSDF')
    bs.inputs['Base Color'].default_value = (*color, 1)
    bs.inputs['Roughness'].default_value = rough
    bs.inputs['Metallic'].default_value = metal
    if emission:
        bs.inputs['Emission Color'].default_value = (*color, 1)
        bs.inputs['Emission Strength'].default_value = emission
    return m

ivory = material('ABS | warm ivory', (.57,.48,.33), .43)
edge = material('ABS | exposed bevel', (.72,.64,.48), .48)
dark = material('Graphite rubber', (.018,.021,.019), .72)
seam = material('Recesses', (.046,.041,.030), .7)
steel = material('Olive enamel', (.13,.145,.12), .43,.45)
steel_edge = material('Worn enamel edge', (.26,.26,.19), .38,.55)
brass = material('Brushed nickel brass', (.37,.29,.15), .3,.78)
black = material('Bakelite', (.017,.021,.020), .24)
paper = material('Aged paper', (.73,.64,.44), .9)
ink = material('Print', (.10,.075,.042), .8)
blue = material('CRT phosphor', (.018,.033,.23), .26,0, .65)
green = material('Power LED', (.21,.65,.12), .2,0, 3)
amber = material('Amber LED', (.95,.24,.024), .2,0, 2)
red = material('Oxblood tape', (.16,.027,.018), .6)
floor = material('Concrete', (.035,.040,.036), .85)
wall = material('Painted wall', (.041,.047,.042), .95)

def box(name, loc, size, mat, bevel=.01, parent=None):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    o=bpy.context.object; o.name=name; o.dimensions=size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if bevel:
        mod=o.modifiers.new('Machined edge', 'BEVEL'); mod.width=bevel; mod.segments=3
        bpy.context.view_layer.objects.active=o; bpy.ops.object.modifier_apply(modifier=mod.name)
        mod=o.modifiers.new('Weighted normals','WEIGHTED_NORMAL')
        bpy.ops.object.modifier_apply(modifier=mod.name)
    o.data.materials.append(mat)
    if parent: o.parent=parent
    return o

def cylinder(name,loc,radius,depth,mat,rotation=(0,0,0),vertices=32):
    bpy.ops.mesh.primitive_cylinder_add(vertices=vertices,radius=radius,depth=depth,location=loc,rotation=rotation)
    o=bpy.context.object;o.name=name;o.data.materials.append(mat)
    bevel=o.modifiers.new('Rim','BEVEL');bevel.width=min(.006,depth*.18);bevel.segments=2
    bpy.ops.object.modifier_apply(modifier=bevel.name)
    for f in o.data.polygons:f.use_smooth=True
    return o

def tube(name, points, radius, mat):
    curve=bpy.data.curves.new(name,'CURVE');curve.dimensions='3D';curve.resolution_u=2
    spline=curve.splines.new('POLY');spline.points.add(len(points)-1)
    for p,xyz in zip(spline.points,points):p.co=(*xyz,1)
    curve.bevel_depth=radius;curve.bevel_resolution=3
    o=bpy.data.objects.new(name,curve);bpy.context.collection.objects.link(o);o.data.materials.append(mat)
    return o

def text(name, words, loc, size, mat, rotation=(math.pi/2,0,0)):
    curve=bpy.data.curves.new(name,'FONT');curve.body=words;curve.size=size;curve.extrude=.0003;curve.resolution_u=2
    o=bpy.data.objects.new(name,curve);bpy.context.collection.objects.link(o);o.location=loc;o.rotation_euler=rotation;o.data.materials.append(mat)
    return o

def screw(loc,front=True):
    x,y,z=loc
    cylinder('Recessed screw',loc,.009,.003,brass,(math.pi/2,0,0) if front else (0,0,0),16)
    box('Screw slot',(x,y-.002,z) if front else (x,y,z+.002),(.012,.001,.002) if front else (.012,.002,.001),dark,0)

# Twin-pedestal steel desk, real drawer reveals and hardware.
box('DESK | rolled top',(0,-.20,1.02),(3.25,1.76,.075),steel_edge,.025)
box('DESK | inset working surface',(0,-.20,1.061),(3.18,1.69,.009),steel,.009)
box('Desk front lip',(0,-1.077,1.0),(3.17,.018,.055),brass,.005)
for x in (-1.21,1.21):
    box('Pedestal carcass',(x,.03,.51),(.67,1.15,.94),steel,.018)
    box('Plinth',(x,.035,.072),(.72,1.18,.11),seam,.012)
    for z,h in ((.825,.24),(.51,.365),(.19,.24)):
        box('Drawer shadow reveal',(x,-.554,z),(.618,.01,h),seam,.003)
        box('Drawer enamel fascia',(x,-.569,z),(.595,.025,h-.016),steel_edge,.008)
        for dx in (-.105,.105):
            cylinder('Handle foot',(x+dx,-.592,z+.025),.019,.018,brass,(math.pi/2,0,0))
        tube('Cast drawer pull',[(x-.105,-.59,z+.025),(x-.105,-.636,z+.016),(x+.105,-.636,z+.016),(x+.105,-.59,z+.025)],.009,brass)
        box('Label holder',(x,-.587,z-.06),(.125,.008,.042),brass,.003)
        box('Label card',(x,-.593,z-.06),(.107,.002,.025),paper,.001)
    cylinder('Cabinet lock',(x+.22,-.59,.895),.016,.006,brass,(math.pi/2,0,0))
    for dx in (-.24,.24):
        for y in (-.39,.45):cylinder('Adjustable foot',(x+dx,y,.025),.035,.04,dark)
box('Desk modesty panel',(0,.5,.64),(1.8,.036,.64),steel,.005)
box('Desk cross member',(0,.46,.18),(2.5,.07,.06),steel_edge,.008)

# Horizontal system unit, ports, floppy drives, case seam, ventilation.
box('PC | system unit',(0,.02,1.20),(1.30,.78,.255),ivory,.026)
box('PC | lower chassis seam',(0,.019,1.102),(1.31,.785,.018),seam,.007)
box('PC | front fascia',(0,-.381,1.218),(1.245,.031,.172),edge,.01)
for x in (.23,.47):
    box('5.25 inch drive',(x,-.404,1.233),(.215,.021,.105),ivory,.006)
    box('Floppy slot',(x,-.419,1.246),(.181,.007,.014),dark,.002)
    box('Drive lever',(x+.059,-.432,1.213),(.031,.018,.034),brass,.003)
    box('Drive activity',(x-.073,-.429,1.214),(.014,.008,.005),amber,.001)
for i in range(11):box('System front vent',(-.50+i*.044,-.404,1.208),(.023,.009,.082),seam,.003)
cylinder('Power button',(-.575,-.408,1.217),.023,.01,brass,(math.pi/2,0,0))
text('Case identity','SHORTSLAB / SYSTEM 84',(-.48,-.419,1.277),.018,ink)
for side in (-1,1):
    for j in range(13):box('Side grille',(side*.652,-.19+j*.037,1.23),(.004,.018,.11),seam,.002)

# CRT: four physically separate bezel bars make a deep window, not a flat decal.
box('CRT | swivel foot',(0,.11,1.366),(.61,.49,.082),seam,.038)
box('CRT | pedestal',(0,.11,1.423),(.37,.29,.09),ivory,.025)
box('CRT | rear enclosure',(0,.27,1.84),(.94,.69,.82),ivory,.085)
box('CRT | back service cover',(0,.619,1.83),(.73,.024,.60),edge,.035)
box('CRT | front shadow gasket',(0,-.116,1.857),(1.10,.041,.887),seam,.036)
box('CRT | bezel top',(0,-.152,2.279),(1.12,.125,.104),edge,.028)
box('CRT | bezel chin',(0,-.17,1.419),(1.12,.15,.147),edge,.026)
box('CRT | bezel left',(-.526,-.156,1.862),(.091,.13,.79),edge,.026)
box('CRT | bezel right',(.526,-.156,1.862),(.091,.13,.79),edge,.026)
# Inner shadow tunnel lips create thickness in the screen reveal.
for x in (-.468,.468):box('CRT | inner side lip',(x,-.17,1.877),(.024,.06,.72),seam,.007)
for z in (1.51,2.239):box('CRT | inner rim',(0,-.17,z),(.93,.06,.022),seam,.006)

# Convex CRT mesh with continuous UV coordinates, used for live CanvasTexture in Three.
verts=[];uvs=[];faces=[]
nx,ny=40,32
for row in range(ny+1):
    v=row/ny; z=1.525+v*.694
    for col in range(nx+1):
        u=col/nx;x=(u-.5)*.906
        bulge=.038*(1-(2*u-1)**2)*(1-(2*v-1)**2)
        verts.append((x,-.195-bulge,z));uvs.append((u,v))
for row in range(ny):
    for col in range(nx):
        a=row*(nx+1)+col;faces.append((a,a+1,a+nx+2,a+nx+1))
mesh=bpy.data.meshes.new('Curved CRT glass');mesh.from_pydata(verts,[],faces);mesh.update()
o=bpy.data.objects.new('SCREEN_DISPLAY',mesh);bpy.context.collection.objects.link(o);o.data.materials.append(blue)
uv=mesh.uv_layers.new(name='UVMap')
for poly in mesh.polygons:
    poly.use_smooth=True
    for li in poly.loop_indices:uv.data[li].uv=uvs[mesh.loops[li].vertex_index]
text('Monitor wordmark','SHORTSLAB',(-.425,-.251,1.406),.029,ink)
text('Monitor model','COLOR DISPLAY / 84',(-.425,-.252,1.381),.009,ink)
for i,c in enumerate(((.8,.12,.06),(.94,.45,.08),(.8,.7,.1),(.16,.5,.22),(.1,.23,.65))):
    box('Five stripe badge',(-.468,-.25,1.438-i*.008),(.039,.003,.005),material('Stripe '+str(i),c),.001)
for x in (.28,.36):cylinder('CRT adjustment knob',(x,-.255,1.416),.021,.012,ivory,(math.pi/2,0,0))
cylinder('CRT power LED',(.446,-.255,1.418),.006,.004,green,(math.pi/2,0,0),16)
for side in (-1,1):
    for j in range(15):box('CRT side cooling slot',(side*.468,.11+j*.026,1.81),(.005,.011,.19),seam,.001)
for i in range(17):box('CRT top cooling slot',(-.33+i*.041,.36,2.251),(.015,.20,.003),seam,.002)
for x in (-.32,.32):
    for z in (1.60,2.08):screw((x,.636,z))

# Sculpted keyboard with distinct keycap rows, legends, stabilised large keys.
box('Keyboard | lower shell',(0,-.77,1.112),(1.29,.47,.095),ivory,.023)
box('Keyboard | keywell',(0,-.775,1.164),(1.20,.394,.018),seam,.009)
rows=['1234567890-=', 'QWERTYUIOP[]', 'ASDFGHJKL;', 'ZXCVBNM,./']
for r,letters in enumerate(rows):
    y=-.64-r*.071
    for c,char in enumerate(letters):
        x=-.53+c*.067+(r%2)*.015
        z=1.19-r*.005
        box('Key '+char,(x,y,z),(.059,.062,.045),edge if r!=0 else ivory,.009)
        text('Legend '+char,char,(x-.011,y-.012,z+.024),.018,ink,(0,0,0))
for i,label in enumerate(('ESC','F1','F2','F3')):
    x=.33+i*.066
    box('Function key',(x,-.67,1.19),(.057,.067,.043),ivory,.008)
    text('Function legend',label,(x-.020,-.679,1.215),.011,ink,(0,0,0))
for r in range(3):
    for c in range(3):box('Number pad',(.345+c*.073,-.75-r*.072,1.185),(.064,.063,.044),edge,.008)
box('Space bar',(-.135,-.955,1.171),(.49,.055,.036),edge,.007)
for x in (-.52,-.44,.18,.255):box('Modifier key',(x,-.955,1.171),(.065,.055,.036),ivory,.007)
for x in (.40,.45,.50):box('Keyboard LED',(x,-.60,1.185),(.014,.008,.005),green,.001)
tube('Coiled keyboard lead',[(-.59+.013*math.sin(i*.6),-.4+i*.008,1.14+.013*math.cos(i*.6)) for i in range(52)],.006,dark)

# Tape library, offset sleeves and handwritten index labels.
for i in range(5):
    x=-1.15+random.uniform(-.035,.035); y=.10+random.uniform(-.02,.02);z=1.105+i*.09
    box('VHS archive case',(x,y,z),(.51,.29,.081),black if i%2 else red,.009)
    box('Tape spine label',(x,y-.15,z),(.31,.004,.048),paper,.002)
    text('Archive index',f'{i+1:02d} / SHORTSLAB',(x-.132,y-.154,z-.012),.017,ink)
    for dx in (-.21,.21):screw((x+dx,y-.151,z))
text('Tape top title','THE ARCHIVE',(-1.30,.08,1.51),.030,paper,(0,0,0))

# Bakelite rotary telephone with actual dial holes and a coiled cord.
tx,ty=1.12,.18
box('Telephone | plinth',(tx,ty,1.116),(.55,.42,.075),black,.07)
box('Telephone | body',(tx,ty+.015,1.197),(.46,.34,.13),black,.065)
cylinder('Dial metal rim',(tx,ty-.03,1.272),.139,.02,brass)
cylinder('Dial plate',(tx,ty-.03,1.284),.122,.016,black)
cylinder('Dial centre',(tx,ty-.03,1.296),.061,.012,paper)
text('Dial centre print','SHORTSLAB',(tx-.045,ty-.033,1.304),.012,ink,(0,0,0))
for i in range(10):
    a=math.radians(35+i*28);x=tx+math.cos(a)*.093;y=ty-.03+math.sin(a)*.093
    cylinder('Rotary finger recess',(x,y,1.298),.016,.004,seam,vertices=20)
    text('Dial number',str((i+1)%10),(x-.005,y-.005,1.301),.012,paper,(0,0,0))
for dx in (-.20,.20):
    box('Handset cradle',(tx+dx,ty+.095,1.304),(.059,.10,.10),black,.022)
    cylinder('Handset earpiece',(tx+dx,ty+.12,1.364),.074,.07,black)
tube('Handset curved grip',[(tx-.22+i*.011,ty+.12,1.386+.05*math.sin(i/40*math.pi)) for i in range(41)],.039,black)
tube('Telephone spiral cord',[(tx-.25-.10*i/180+.022*math.cos(i*.75),ty+.08-.48*i/180,1.34-.22*i/180+.022*math.sin(i*.75)) for i in range(181)],.004,black)
tube('Telephone lead',[(.82,-.40,1.10),(.93,-.64,1.08),(.99,-.71,.79),(1.02,-.69,.38),(1.09,-.38,.045),(1.40,.6,.04)],.008,dark)

# Practical desk lamp. Separate reflector shell and luminous diffuser.
cylinder('Lamp base',(-1.22,.43,1.10),.15,.055,steel_edge)
tube('Lamp articulated arm',[(-1.22,.43,1.12),(-1.43,.43,1.57),(-1.14,.38,1.99)],.018,brass)
for x,z in ((-1.22,1.13),(-1.43,1.57),(-1.14,1.99)):
    cylinder('Lamp hinge',(x,.43,z),.038,.07,black,(math.pi/2,0,0))
lamp=box('Lamp reflector',(-1.04,.25,2.00),(.37,.29,.10),steel_edge,.045)
box('Lamp warm diffuser',(-1.04,.25,1.945),(.30,.22,.007),material('Lamp light',(1,.62,.29),.6,0,4),.025)

# Small physical details: note, pencil, cable runs, distressed top inlay.
note=box('Operator note',(.79,-.43,1.071),(.21,.22,.003),paper,.001);note.rotation_euler.z=-.16
text('Operator note print','MAKE SOMETHING\nWORTH WATCHING.',(.696,-.44,1.075),.012,ink,(0,0,-.16))
pencil=cylinder('Graphite pencil',(.95,-.38,1.080),.005,.24,brass,(0,math.pi/2,.5),8)
for i in range(45):
    x=random.uniform(-1.54,1.54);y=random.choice((-1.04,.63))+random.uniform(-.007,.007)
    box('Enamel patina chip',(x,y,1.066),(random.uniform(.008,.048),.003,.001),brass,0)
tube('CRT power cable',[(.25,.57,1.46),(.40,.70,1.22),(.48,.78,.75),(.54,.8,.05),(.9,1.8,.03)],.008,dark)

# Real room geometry, restrained enough to leave the carousel typography readable.
box('ROOM | floor',(0,0,-.032),(40,40,.05),floor,.001)
box('ROOM | rear wall',(0,2.2,2.6),(40,.1,5.3),wall,.001)
box('ROOM | skirting',(0,2.13,.10),(40,.025,.17),steel,.002)
for x in (-3,0,3):box('Wall panel seam',(x,2.139,2.5),(.008,.004,5.0),seam,0)

# Save source and export actual meshes (curves and type converted by exporter).
scene=bpy.context.scene
world=bpy.data.worlds.new('Night studio') if not bpy.data.worlds else bpy.data.worlds[0]
scene.world=world;world.use_nodes=True;world.node_tree.nodes['Background'].inputs[0].default_value=(.09,.10,.12,1);world.node_tree.nodes['Background'].inputs[1].default_value=.22
def light(name, loc, power, color, size, target):
    data=bpy.data.lights.new(name,'AREA');data.energy=power;data.color=color;data.shape='DISK';data.size=size
    obj=bpy.data.objects.new(name,data);scene.collection.objects.link(obj);obj.location=loc;obj.rotation_euler=(Vector(target)-obj.location).to_track_quat('-Z','Y').to_euler()
light('KEY | warm softbox',(-3,-3,5),430,(1,.68,.38),4,(0,0,1))
light('RIM | cold window',(3,1.2,3.8),520,(.30,.65,1),3,(0,0,1.2))
light('FILL | front',(.2,-4,2.5),90,(.61,.72,1),3,(0,0,1.4))
light('Practical lamp',(-1.04,.25,1.93),15,(1,.51,.18),.23,(-1,.1,1.06))
bpy.ops.object.camera_add(location=(3.4,-6.8,3.2))
camera=bpy.context.object;camera.name='SHOWROOM_CAMERA';camera.rotation_euler=(Vector((0,0,1.25))-camera.location).to_track_quat('-Z','Y').to_euler();camera.data.lens=50;scene.camera=camera
scene.render.engine='CYCLES';scene.cycles.samples=48;scene.cycles.use_denoising=True
scene.render.resolution_x=1600;scene.render.resolution_y=1100;scene.render.resolution_percentage=100
scene.view_settings.view_transform='AgX'
bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'shortslab-retro.blend'))
bpy.ops.export_scene.gltf(filepath=str(OUT/'shortslab-retro.glb'),export_format='GLB',export_cameras=False,export_lights=False,export_apply=True,export_yup=True)
print('SHOWROOM_EXPORT_READY',len(bpy.data.objects),flush=True)
if '--render' in sys.argv:
    scene.render.filepath=str(OUT/'reference.png');bpy.ops.render.render(write_still=True)
