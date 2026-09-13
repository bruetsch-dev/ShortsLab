"""Five Blender-built sets with independently choreographable object groups."""
from pathlib import Path
import bpy
import math

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'static/showroom3d'
source = (Path(__file__).with_name('build_scene.py')).read_text(encoding='utf-8')
# Reuse the original modeled components, without overwriting the original source.
prefix = source.split('# Twin-pedestal')[0]
exec(compile(prefix, str(Path(__file__).with_name('build_scene.py')), 'exec'))

def section(start, end):
    return source[source.index(start):source.index(end)]

def group(name, build):
    before = set(bpy.data.objects)
    build()
    objects = set(bpy.data.objects) - before
    root = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(root)
    # Pivot about the physical object's centre, so rotation does not orbit origin.
    if objects:
        root.location = sum((o.location for o in objects), Vector()) / len(objects)
    bpy.context.view_layer.update()
    for o in objects:
        world = o.matrix_world.copy()
        o.parent = root
        o.matrix_world = world
    return root

def run(code):
    exec(code, globals())

def new_desk(kind):
    top = wood if kind == 'longform' else lacquer if kind == 'enhance' else alloy
    box('Worktop '+kind,(0,-.20,1.025),(3.25,1.76,.085),top,.025)
    if kind == 'longform':
        for x in (-1.18,1.18):
            for side in (-1,1):
                tube('Drafting trestle',[(x+side*.29,.2,.04),(x,.2,.99)],.047,wood)
            box('Trestle runner',(x,.08,.055),(.79,1.20,.08),wood,.012)
        tube('Drafting cross brace',[(-1.18,.2,.40),(1.18,.2,.40)],.035,brass)
        for i in range(18):
            box('Maple grain',(-1.5+i*.175,-.20,1.069),(.003,1.66,.001),paper,0)
    elif kind == 'ai':
        for x in (-1.30,1.30):
            box('Cantilever upright',(x,.40,.53),(.085,.10,1.0),chrome,.02)
            box('Cantilever foot',(x,-.15,.08),(.13,1.35,.10),chrome,.03)
        box('Floating drawer',(-1.04,-.05,.88),(.84,1.12,.23),alloy,.035)
        box('Drawer recessed grip',(-1.04,-.617,.88),(.42,.01,.025),dark,.005)
    elif kind == 'physics':
        for x in (-1.38,1.38):
            for y in (-.78,.43):
                box('Laboratory leg',(x,y,.53),(.09,.09,1.0),steel,.008)
            box('Lab foot brace',(x,-.17,.20),(.07,1.3,.07),steel,.005)
        box('Instrument shelf',(0,.48,.29),(2.82,.32,.055),steel,.007)
        box('Bench power rail',(0,.62,1.15),(3.12,.09,.14),edge,.008)
        for x in (-1.2,-.8,-.4,.4,.8,1.2):
            cylinder('Rail socket',(x,.567,1.15),.027,.009,dark,(math.pi/2,0,0))
    else:
        for x in (-1.17,1.17):
            box('Studio rack pedestal',(x,.03,.55),(.73,1.15,1.0),lacquer,.026)
            for i in range(5):
                z=.19+i*.16
                box('Rack processor',(x,-.56,z),(.64,.04,.14),dark,.005)
                for dx in (-.23,-.12,.12,.23):
                    cylinder('Rack gain dial',(x+dx,-.595,z),.018,.018,chrome,(math.pi/2,0,0))
                box('Rack meter',(x,-.587,z),(.10,.008,.042),green,.002)
        box('Padded console edge',(0,-1.06,1.07),(3.16,.12,.10),black,.035)

def computer(kind):
    if kind == 'clip':
        run(section('# Horizontal system', '# CRT:'))
    elif kind == 'ai':
        box('Vision integrated base',(0,.02,1.23),(1.13,.86,.31),alloy,.055)
        for i in range(15):
            box('Vision intake',(-.43+i*.060,-.42,1.22),(.025,.009,.10),dark,.002)
        box('Vision optical slot',(0,-.425,1.31),(.58,.012,.011),dark,.002)
    elif kind == 'longform':
        box('Sketch wedge system',(0,.02,1.23),(1.35,.81,.29),ivory,.045)
        box('Sketch cartridge bay',(.33,-.4,1.23),(.46,.03,.12),seam,.008)
        text('Sketch badge','DRAFT / 128',(-.53,-.418,1.26),.030,ink)
    elif kind == 'physics':
        box('Instrument computer',(0,.02,1.23),(1.43,.86,.30),steel,.012)
        for x in (-.60,.60):
            tube('Instrument carry handle',[(x,-.43,1.13),(x,-.48,1.13),(x,-.48,1.34),(x,-.43,1.34)],.015,chrome)
        for i in range(6):
            cylinder('Computer toggle',(-.40+i*.14,-.437,1.23),.017,.022,brass,(math.pi/2,0,0))
    else:
        box('Mastering workstation',(0,.02,1.23),(1.40,.85,.30),black,.025)
        for x in (-.49,.49):
            box('Workstation drive',(x,-.42,1.25),(.30,.02,.14),chrome,.005)
        text('Mastering badge','REFERENCE / PRO',(-.22,-.437,1.24),.028,paper)
    run(section('# CRT:', '# Sculpted keyboard'))
    # Keep the glass aperture at the shared interaction target, change the case silhouette.
    if kind != 'clip':
        for o in list(bpy.data.objects):
            if o.name.startswith('CRT | rear enclosure') and o.parent is None:
                o.scale.x = {'ai':1.20,'longform':.98,'physics':1.28,'enhance':1.13}[kind]
                o.scale.y = {'ai':.66,'longform':1.15,'physics':.90,'enhance':.76}[kind]
        if kind == 'ai':
            box('Integrated left cheek',(-.59,.03,1.78),(.12,.55,.99),alloy,.045)
            box('Integrated right cheek',(.59,.03,1.78),(.12,.55,.99),alloy,.045)
        if kind == 'physics':
            for x in (-.62,.62):
                box('Instrument side rail',(x,-.15,1.86),(.08,.14,.92),steel,.01)
        if kind == 'enhance':
            box('Studio monitor hood',(0,-.12,2.36),(1.30,.64,.065),black,.012)

def camera_prop():
    box('Camera body',(-1.12,.05,1.28),(.43,.26,.30),black,.025)
    cylinder('Camera lens',(-1.12,-.16,1.29),.11,.22,chrome,(math.pi/2,0,0))
    cylinder('Lens glass',(-1.12,-.279,1.29),.086,.015,blue,(math.pi/2,0,0))
    box('Viewfinder',(-1.12,.03,1.465),(.17,.18,.09),alloy,.015)

def orb_prop():
    cylinder('Optical pedestal',(1.15,.16,1.10),.20,.06,chrome)
    for i in range(3):
        points=[]
        for j in range(97):
            a=j*math.tau/96
            v=Vector((math.cos(a)*.24,math.sin(a)*.24,0))
            from mathutils import Euler
            v.rotate(Euler((i*.85,.65,0)))
            points.append((v.x+1.15,v.y+.16,v.z+1.43))
        tube('Orbital study',points,.008,brass)

def sketch_pad():
    box('Drawing board',(-1.12,-.15,1.085),(.64,.92,.027),wood,.008)
    box('Drawing paper',(-1.12,-.15,1.105),(.57,.79,.003),paper,.001)
    for i in range(3):
        y=-.41+i*.24
        tube('Storyboard panel',[(-1.35,y,1.108),(-.9,y,1.108),(-.9,y+.19,1.108),(-1.35,y+.19,1.108),(-1.35,y,1.108)],.002,ink)
        cylinder('Storyboard head',(-1.14,y+.12,1.11),.025,.002,ink)
        tube('Storyboard figure',[(-1.14,y+.10,1.11),(-1.14,y+.055,1.11),(-1.19,y+.025,1.11)],.003,ink)

def pencil_cup():
    cylinder('Ceramic pencil cup',(1.17,.17,1.20),.10,.26,red)
    for i in range(7):
        cylinder('Drawing pencil',(1.12+(i%3)*.045,.12+(i//3)*.044,1.39),.006,.31,brass,(.05*i,.03*i,0),8)
    for i in range(3):
        box('Sketchbook',(1.15,-.26,1.10+i*.035),(.53,.38,.031),paper if i%2 else red,.003)

def scope():
    box('Oscilloscope',(-1.12,.08,1.30),(.61,.48,.44),ivory,.027)
    box('Scope glass',(-1.22,-.17,1.31),(.29,.009,.25),dark,.015)
    tube('Scope waveform',[(-1.35+i*.004,-.18,1.31+math.sin(i*.19)*.073) for i in range(65)],.003,green)
    for z in (1.20,1.32,1.44):
        cylinder('Scope dial',(-.91,-.19,z),.026,.025,black,(math.pi/2,0,0))

def cradle():
    box('Pendulum base',(1.14,.12,1.10),(.64,.42,.06),wood,.014)
    for y in (-.04,.28):
        tube('Pendulum frame',[(.87,y,1.13),(.87,y,1.65),(1.41,y,1.65),(1.41,y,1.13)],.012,chrome)
    for i in range(5):
        x=.96+i*.091
        for y in (-.04,.28):tube('Pendulum suspension',[(x,y,1.65),(x,.12,1.30)],.0018,dark)
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24,ring_count=12,radius=.045,location=(x,.12,1.30))
        bpy.context.object.name='Pendulum ball';bpy.context.object.data.materials.append(chrome)

def speaker(x):
    box('Studio speaker',(x,.13,1.43),(.44,.39,.70),wood,.016)
    box('Speaker baffle',(x,-.074,1.43),(.40,.015,.65),black,.004)
    for z,r in ((1.32,.137),(1.62,.06)):
        cylinder('Speaker cone',(x,-.09,z),r,.025,dark,(math.pi/2,0,0))
        cylinder('Speaker centre',(x,-.109,z),r*.38,.018,chrome,(math.pi/2,0,0))

def mixer():
    box('Fader controller',(1.10,-.58,1.12),(.58,.53,.10),black,.016)
    for i in range(6):
        x=.88+i*.086
        box('Fader slot',(x,-.60,1.174),(.009,.25,.003),chrome,.001)
        box('Fader cap',(x,-.65+(i%3)*.045,1.188),(.05,.035,.024),edge,.003)
        cylinder('Channel dial',(x,-.40,1.185),.018,.026,brass)

wood=material('Maple hardwood',(.32,.16,.066),.55)
chrome=material('Satin aluminium',(.44,.47,.44),.28,.8)
alloy=material('Vision enamel',(.33,.43,.39),.35,.25)
lacquer=material('Studio walnut',(.09,.037,.021),.42)
palettes={'clip':((.57,.48,.33),(.72,.64,.48)), 'ai':((.30,.40,.37),(.49,.61,.56)), 'longform':((.63,.56,.43),(.79,.72,.56)), 'physics':((.19,.28,.27),(.41,.49,.42)), 'enhance':((.055,.06,.058),(.18,.19,.18))}
for kind in palettes:
    a,b=palettes[kind]
    ivory=material(kind+' case',a,.43);edge=material(kind+' trim',b,.4)
    before=set(bpy.data.objects)
    group('PART_desk',lambda: run(section('# Twin-pedestal','# Horizontal system')) if kind=='clip' else new_desk(kind))
    group('PART_computer',lambda:computer(kind))
    group('PART_keyboard',lambda:run(section('# Sculpted keyboard','# Tape library')))
    if kind=='clip':
        group('PART_archive',lambda:run(section('# Tape library','# Bakelite')))
        group('PART_telephone',lambda:run(section('# Bakelite','# Practical desk')))
        group('PART_lamp',lambda:run(section('# Practical desk','# Small physical')))
        group('PART_note',lambda:run(section('# Small physical','# Real room')))
    elif kind=='ai':
        group('PART_camera',camera_prop);group('PART_orb',orb_prop)
    elif kind=='longform':
        group('PART_storyboard',sketch_pad);group('PART_pencils',pencil_cup)
    elif kind=='physics':
        group('PART_scope',scope);group('PART_pendulum',cradle)
    else:
        group('PART_speaker_left',lambda:speaker(-1.12));group('PART_speaker_right',lambda:speaker(1.12));group('PART_mixer',mixer)
    station=bpy.data.objects.new('STATION_'+kind,None);bpy.context.collection.objects.link(station)
    for obj in set(bpy.data.objects)-before-{station}:
        if obj.parent is None:obj.parent=station
run(section('# Real room','# Save source'))
bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'shortslab-stations.blend'))
bpy.ops.export_scene.gltf(filepath=str(OUT/'shortslab-stations.glb'),export_format='GLB',export_cameras=False,export_lights=False,export_apply=True,export_yup=True)
print('STATIONS_READY',len(bpy.data.objects),flush=True)
