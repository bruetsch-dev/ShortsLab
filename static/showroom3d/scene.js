import * as THREE from './vendor/three.module.js';
import { GLTFLoader } from './vendor/GLTFLoader.js';
import { createLens, ease, flightPosition } from './cinematic.js';
import { drawInstrument } from './crt-display.js';
import { finishMaterials } from './material-finish.js?v=2';

// Blender is the source of every piece of furniture. This module supplies light,
// live CRT content, interaction and the camera; it never rebuilds the set in JS.
export async function createShowroom(host, initialMode, onEnter) {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const arrivalColor=getComputedStyle(document.body).getPropertyValue('--stage').trim()||'#111313';
  host.style.setProperty('--arrival-color',arrivalColor);
  const renderer = new THREE.WebGLRenderer({antialias:true, alpha:false, powerPreference:'high-performance'});
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.VSMShadowMap;
  // Shadows are refreshed while individual set pieces assemble, then cached.
  renderer.shadowMap.autoUpdate = false;
  renderer.shadowMap.needsUpdate = true;
  renderer.domElement.className = 'showroom-webgl';
  renderer.domElement.setAttribute('aria-hidden','true');
  const lens=createLens(renderer);
  host.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#100f0e');
  scene.fog = new THREE.FogExp2('#100f0e', .052);
  const camera = new THREE.PerspectiveCamera(36,1,.035,50);
  const target = new THREE.Vector3(), home = new THREE.Vector3();
  let width=1,height=1,disposed=false,entering=false,frame=0,mode=null,last=0;
  let pointer = new THREE.Vector2(), smooth = new THREE.Vector2(), flight=null;
  let carousel=null,pendingFlight=null;
  let revealStart=0,baseFov=36,shotOffset=0,shotGoal=0,look=new THREE.Vector3();
  let stationStart=0,stationDirection=1;
  const glowGoal=new THREE.Color('#9cb9ff');
  const rimGoal=new THREE.Color('#78b4d8');
  const hemi = new THREE.HemisphereLight('#b0c0d5','#262923',.85); scene.add(hemi);
  const key = new THREE.DirectionalLight('#ffe4c4',4.1);key.position.set(-3,5,4);key.castShadow=true;
  key.shadow.mapSize.set(2048,2048);key.shadow.camera.left=-4;key.shadow.camera.right=4;
  key.shadow.radius=6.5;key.shadow.blurSamples=12;
  key.shadow.camera.top=4;key.shadow.camera.bottom=-3;key.shadow.camera.near=.1;key.shadow.camera.far=15;
  key.shadow.normalBias=.015;key.shadow.bias=-.00025;scene.add(key);
  const rim = new THREE.DirectionalLight('#78b4d8',2.6);rim.position.set(4,3,-2);scene.add(rim);
  const fill = new THREE.DirectionalLight('#ded5be',.55);fill.position.set(0,2,5);scene.add(fill);
  const practical = new THREE.PointLight('#ffad50',.65,2,2);practical.position.set(-1.04,1.91,-.25);scene.add(practical);
  const glow = new THREE.PointLight('#253eff',.26,1.8,2);glow.position.set(0,1.85,.44);scene.add(glow);
  const floorPractical=new THREE.PointLight('#ffd49a',2.0,3.0,2);
  floorPractical.position.set(-1.82,2.12,-.65);scene.add(floorPractical);
  const loader = new GLTFLoader();
  let gltf;
  try {gltf=await loader.loadAsync(new URL('./shortslab-stations.glb',import.meta.url).href, event=>{
    const label=host.querySelector('.blender-load');
    if(event.lengthComputable){
      const progress=Math.min(1,event.loaded/event.total);
      host.style.setProperty('--model-progress',String(progress));
      if(label)label.textContent=progress===1?'Setting the lights':'Preparing your station';
    }
  });}
  catch(error){lens.dispose();renderer.dispose();renderer.domElement.remove();throw error;}
  const model = gltf.scene;
  finishMaterials(model);
  // Broad studio softboxes give the authored metal and plastic actual reflected
  // shapes. Direct lights alone left the metallic desk faces almost black.
  const lightStage=new THREE.Scene();
  lightStage.background=new THREE.Color('#252b30');
  const softboxes=[];
  function softbox(w,h,position,color,intensity){
    const surface=new THREE.Mesh(new THREE.PlaneGeometry(w,h),new THREE.MeshBasicMaterial({color:new THREE.Color(color).multiplyScalar(intensity),side:THREE.DoubleSide}));
    surface.position.set(...position);surface.lookAt(0,1,0);lightStage.add(surface);softboxes.push(surface);
  }
  softbox(5,3,[-4,5,4],'#fff0db',3);
  softbox(2,5,[4,3,1],'#d8e6ee',2);
  softbox(4,1,[0,5,-3],'#ffffff',2.5);
  const pmrem=new THREE.PMREMGenerator(renderer);
  const studioEnvironment=pmrem.fromScene(lightStage,.025,.1,30);
  scene.environment=studioEnvironment.texture;scene.environmentIntensity=.65;
  pmrem.dispose();softboxes.forEach(o=>{o.geometry.dispose();o.material.dispose();});
  model.traverse(o=>{if(o.isMesh){o.castShadow=!o.name.startsWith('ROOM');o.receiveShadow=!/rear.*wall/i.test(o.name);}});
  scene.add(model);
  // The surrounding architecture stays in place while workstation pieces arrive.
  let roomModel=null;
  try {
    const room=await loader.loadAsync(new URL('./shortslab-room.glb?v=review3',import.meta.url).href);
    roomModel=room.scene;
    finishMaterials(roomModel);
    roomModel.traverse(o=>{if(o.isMesh){const p=new THREE.Vector3();o.getWorldPosition(p);const overhead=p.y>=3||/diffuser/.test(o.name);o.castShadow=!overhead;o.receiveShadow=!overhead;}});
    scene.add(roomModel);
  } catch(error) {console.warn('Studio surroundings unavailable',error);}
  const screens=[];
  model.traverse(o=>{if(o.isMesh && o.name.startsWith('SCREEN_DISPLAY'))screens.push(o);});
  const screen = screens[0];
  const stations=new Map();
  model.children.filter(o=>o.name.startsWith('STATION_')).forEach(root=>{
    const order=name=>name.startsWith('PART_desk')?0:name.startsWith('PART_computer')?1:name.startsWith('PART_keyboard')?2:3;
    const pieces=[...root.children].sort((a,b)=>order(a.name)-order(b.name)).map((object,index)=>({object,index,position:object.position.clone(),rotation:object.rotation.clone()}));
    stations.set(root.name.slice(8),{root,pieces});root.visible=false;
  });
  let assembly=null;
  function assemble(id,direction=1){
    for(const [key,station] of stations)station.root.visible=key===id;
    const station=stations.get(id);
    if(!station)throw new Error('Missing Blender station: '+id);
    assembly={station,start:performance.now(),direction};
    updateAssembly(assembly.start);
    host.dataset.station=id;
  }
  function updateAssembly(t,finish=false){
    if(!assembly)return;
    const {station,start,direction}=assembly;
    let complete=true;
    for(const piece of station.pieces){
      const {object,index,position,rotation}=piece;
      const desk=object.name.startsWith('PART_desk');
      const delay=desk?0:240+index*145;
      const p=finish||reduced?1:Math.max(0,Math.min(1,(t-start-delay)/1350));
      complete&&=p===1;
      // `settle` is how far the piece still has to travel, and it never goes negative. The
      // overshoot it had (a cosine that changes sign) carried every object PAST its resting
      // place - which for anything standing on the desk means through the desk top. An ease-out
      // lands on the surface instead of sinking into it.
      const q=1-p;
      const settle=p===1?0:Math.pow(q,3);
      object.position.copy(position);
      // Everything that stands on the desk comes straight down onto it, always from above, one
      // after the other. The alternating left/right offsets and opposite rotations read as
      // debris blown across the frame rather than as a set being laid; and with `direction`
      // applied to a lift, scrolling backwards sent the objects UP THROUGH the desk to land.
      // The desk itself is the only piece that rises, and it has nothing underneath to hit.
      object.position.y+=settle*(desk?-1.65*direction:1.10+index*.11);
      object.position.z+=settle*(desk?-.55:.28);
      object.rotation.copy(rotation);
      object.rotation.z+=settle*(desk?.035:.05);
      object.visible=p>0;
    }
    renderer.shadowMap.needsUpdate=true;
    if(complete)assembly=null;
  }
  if(!screen){
    model.traverse(o=>{if(o.isMesh){o.geometry.dispose();for(const m of [].concat(o.material))m.dispose();}});
    lens.dispose();renderer.dispose();renderer.domElement.remove();
    throw new Error('The Blender model is missing SCREEN_DISPLAY');
  }
  const display = document.createElement('canvas');display.width=1024;display.height=768;
  const ctx=display.getContext('2d');
  const texture=new THREE.CanvasTexture(display);texture.colorSpace=THREE.SRGBColorSpace;texture.flipY=false;
  const crtMaterial = new THREE.MeshBasicMaterial({map:texture,toneMapped:false,side:THREE.DoubleSide});
  screens.forEach(glass=>{glass.material.dispose();glass.material=crtMaterial;});
  const hitButton=document.createElement('button');hitButton.className='showroom-screen-target';hitButton.type='button';
  hitButton.addEventListener('click',()=>{if(!entering)onEnter();});host.appendChild(hitButton);
  const movie=document.createElement('video');movie.muted=true;movie.loop=true;movie.playsInline=true;movie.preload='none';
  const movieTexture=new THREE.VideoTexture(movie);movieTexture.colorSpace=THREE.SRGBColorSpace;
  // Optical falloff makes the preview read as light on plaster, without a frame.
  const maskSize=128,maskBytes=new Uint8Array(maskSize*maskSize*4);
  for(let y=0;y<maskSize;y++)for(let x=0;x<maskSize;x++){
    const u=x/(maskSize-1),v=y/(maskSize-1);
    const edge=Math.min(u,1-u,v,1-v);
    const value=Math.round(255*ease(edge/.18));
    const i=(y*maskSize+x)*4;
    maskBytes[i]=maskBytes[i+1]=maskBytes[i+2]=value;maskBytes[i+3]=255;
  }
  const projectionMask=new THREE.DataTexture(maskBytes,maskSize,maskSize,THREE.RGBAFormat);
  projectionMask.magFilter=THREE.LinearFilter;projectionMask.minFilter=THREE.LinearFilter;projectionMask.needsUpdate=true;
  const projection=new THREE.Mesh(new THREE.PlaneGeometry(5.4,3.04),new THREE.MeshBasicMaterial({map:movieTexture,alphaMap:projectionMask,transparent:true,opacity:.15,depthWrite:false,toneMapped:false}));
  projection.position.set(.2,2.55,-2.137);scene.add(projection);
  const posterLoader=new THREE.TextureLoader();let poster=null,mediaVersion=0,projectionRevealStart=0;
  let screenStart=0,screenFrame=0,screenComplete=false;
  function drawScreen(next){
    screenStart=performance.now();screenFrame=0;screenComplete=reduced;
    drawInstrument(ctx,next,reduced?4000:0);texture.needsUpdate=true;
    hitButton.setAttribute('aria-label','Open '+next.name);
  }
  function clearCarousel(){
    if(!carousel)return;
    carousel.animations.forEach(animation=>animation.cancel());carousel.snapshot.remove();carousel=null;
    hitButton.style.pointerEvents='';
  }
  function rollStation(direction){
    lens.render(scene,camera);
    const snapshot=document.createElement('canvas');snapshot.className='showroom-outgoing';
    snapshot.width=renderer.domElement.width;snapshot.height=renderer.domElement.height;
    const capture=snapshot.getContext('2d');
    if(carousel){
      // Capture the actual in-between composition before cancelling its motion.
      // Reading each current transform also handles a reversal of direction.
      const scale=snapshot.height/height;
      for(const layer of [renderer.domElement,carousel.snapshot]){
        const transform=getComputedStyle(layer).transform;
        const offset=transform==='none'?0:new DOMMatrixReadOnly(transform).m42;
        capture.drawImage(layer,0,offset*scale);
      }
    } else capture.drawImage(renderer.domElement,0,0);
    clearCarousel();
    snapshot.setAttribute('aria-hidden','true');host.appendChild(snapshot);
    const options={duration:1000,easing:'cubic-bezier(.22,.8,.25,1)',fill:'both'};
    const outgoing=snapshot.animate([{transform:'translateY(0)'},{transform:`translateY(${-direction*100}%)`}],options);
    const incoming=renderer.domElement.animate([{transform:`translateY(${direction*100}%)`},{transform:'translateY(0)'}],options);
    carousel={snapshot,animations:[outgoing,incoming]};
    incoming.finished.then(()=>{if(carousel?.snapshot===snapshot)clearCarousel();}).catch(()=>{});
  }
  function setMode(next, options={}){
    if(disposed || entering || mode===next)return;
    if(mode && options.animate && !reduced)rollStation(options.direction<0?-1:1);
    mode=next;drawScreen(next);
    stationStart=performance.now();stationDirection=options.direction<0?-1:1;
    assemble(next.id,options.direction<0?-1:1);
    glowGoal.set(({clip:'#9cb9ff',ai:'#98e2d5',longform:'#edcb87',physics:'#9de5a7',enhance:'#d0b7ee'})[next.id]||'#9cb9ff');
    const shots={clip:[0,'#78b4d8'],ai:[.25,'#99a4df'],longform:[-.20,'#d0af82'],physics:[.16,'#82bbbc'],enhance:[-.10,'#c5a0a0']};
    const shot=shots[next.id]||shots.clip;shotGoal=shot[0];rimGoal.set(shot[1]);
    const requested=++mediaVersion;
    movie.pause();movie.removeAttribute('src');movie.load();
    if(poster){poster.dispose();poster=null;}
    projection.visible=false;
    projectionRevealStart=0;
    function revealProjection(){
      if(disposed || requested!==mediaVersion)return;
      if(document.hidden && !movie.paused){resumeMovie=true;movie.pause();}
      projection.material.opacity=0;
      projectionRevealStart=performance.now();
      projection.visible=false;
    }
    function showPoster(){
      if(disposed || requested!==mediaVersion || !next.poster)return;
      posterLoader.load(new URL('../'+next.poster,import.meta.url).href,t=>{
        if(disposed||requested!==mediaVersion){t.dispose();return;}
        poster=t;t.colorSpace=THREE.SRGBColorSpace;projection.material.map=t;
        projection.material.needsUpdate=true;revealProjection();
      });
    }
    if(false && next.video&&!reduced){
      movie.src=new URL('../'+next.video,import.meta.url).href;projection.material.map=movieTexture;
      movie.play().then(revealProjection).catch(showPoster);
    }
    projection.material.needsUpdate=true;
  }
  function resize(){
    width=host.clientWidth;height=host.clientHeight;if(!width||!height)return;
    renderer.setSize(width,height,false);camera.aspect=width/height;
    lens.resize(width,height);
    const narrow=width<760;
    camera.fov=narrow?40:36;
    // Reserve the left third for the existing carousel's copy.
    if(height<456 && width>height){home.set(2.6,2.65,5.9);target.set(-1.4,1.35,0);camera.fov=36;}
    else if(narrow){home.set(3.4,3.0,10.2);target.set(0,2.10,0);}
    else{home.set(2.7,2.65,6.3);target.set(-.9,1.46,0);}
    baseFov=camera.fov;
    camera.updateProjectionMatrix();
  }
  const corners=[new THREE.Vector3(-.453,1.525,.235),new THREE.Vector3(.453,1.525,.235),new THREE.Vector3(-.453,2.219,.235),new THREE.Vector3(.453,2.219,.235)];
  function updateHit(){
    const pts=corners.map(p=>p.clone().project(camera));
    const xs=pts.map(p=>(p.x*.5+.5)*width),ys=pts.map(p=>(-.5*p.y+.5)*height);
    hitButton.style.cssText=`left:${Math.min(...xs)}px;top:${Math.min(...ys)}px;width:${Math.max(...xs)-Math.min(...xs)}px;height:${Math.max(...ys)-Math.min(...ys)}px;`;
    hitButton.style.pointerEvents=carousel||assembly?'none':'';
  }
  const observer=new ResizeObserver(resize);observer.observe(host);resize();setMode(initialMode);
  const onMove=e=>{const r=host.getBoundingClientRect();pointer.set((e.clientX-r.left)/r.width-.5,(e.clientY-r.top)/r.height-.5);};
  const onLeave=()=>pointer.set(0,0);
  host.addEventListener('pointermove',onMove);host.addEventListener('pointerleave',onLeave);
  let hiddenAt=null,resumeMovie=false,pausedAnimations=[];
  function onVisibility(){
    if(document.hidden){
      if(hiddenAt!==null)return;
      hiddenAt=performance.now();resumeMovie=!movie.paused;movie.pause();
      pausedAnimations=(host.closest('.showroom')?.getAnimations({subtree:true})||[])
        .filter(animation=>animation.playState==='running');
      pausedAnimations.forEach(animation=>animation.pause());
      return;
    }
    if(hiddenAt===null)return;
    const now=performance.now(),pause=now-hiddenAt;hiddenAt=null;
    if(revealStart)revealStart+=pause;
    stationStart+=pause;screenStart+=pause;
    if(projectionRevealStart)projectionRevealStart+=pause;
    if(assembly)assembly.start+=pause;
    if(flight)flight.start+=pause;
    if(pendingFlight)pendingFlight.start+=pause;
    last=now;
    pausedAnimations.forEach(animation=>{if(animation.playState==='paused')animation.play();});
    pausedAnimations=[];
    if(resumeMovie&&!reduced)movie.play().catch(()=>{});
    resumeMovie=false;
  }
  document.addEventListener('visibilitychange',onVisibility);
  function animate(t){
    if(disposed)return;frame=requestAnimationFrame(animate);
    if(document.hidden||t-last<1000/60)return;
    const delta=Math.min(.05,(t-last)/1000);last=t;
    // A click during assembly accelerates the remaining motion continuously.
    // Never teleport the furniture to its final transform before the camera moves.
    updateAssembly(pendingFlight?t+(t-pendingFlight.start)*1.4:t);
    if(!flight && !screenComplete && t-screenFrame>=1000/24){
      const elapsed=t-screenStart;
      drawInstrument(ctx,mode,elapsed);texture.needsUpdate=true;screenFrame=t;
      screenComplete=elapsed>=3600;
    }
    if(pendingFlight && !assembly && !carousel){
      const done=pendingFlight.done;pendingFlight=null;startFlight(done,t);
    }
    if(!revealStart)revealStart=t;
    const reveal=reduced?1:ease((t-revealStart)/3200);
    const arrival=reduced?1:ease((t-stationStart)/2800);
    lens.focus(flight||reduced?0:(1-ease((t-stationStart-350)/1650))*.65);
    const damping=1-Math.exp(-delta*2.1);
    shotOffset+=(shotGoal-shotOffset)*damping;
    rim.color.lerp(rimGoal,damping);
    glow.color.lerp(glowGoal,damping);
    glow.intensity=.12+.14*arrival;
    renderer.toneMappingExposure=.72+.33*reveal;
    const projectionReveal=reduced?1:(projectionRevealStart?ease((t-projectionRevealStart)/900):0);
    projection.material.opacity=.035*reveal*projectionReveal;
    if(flight){
      const f=Math.min(1,(t-flight.start)/2100);const k=ease(f);
      if(f>.18){
        ctx.globalAlpha=1;ctx.drawImage(flight.display,0,0);
        ctx.globalAlpha=ease((f-.18)/.28);ctx.fillStyle=arrivalColor;ctx.fillRect(0,0,1024,768);
        ctx.globalAlpha=1;texture.needsUpdate=true;
      }
      flightPosition(camera.position,flight.from,f);
      const aim=flight.aim.clone().lerp(new THREE.Vector3(0,1.872,.18),k);camera.lookAt(aim);
      host.style.setProperty('--flight',String(ease((f-.80)/.20)));
      if(f>=1){const done=flight.done;flight=null;done();return;}
    } else {
      smooth.lerp(pointer,1-Math.exp(-delta*3));camera.position.copy(home);look.copy(target);
      if(!reduced){
        const time=(t-revealStart)/1000;
        camera.position.x+=shotOffset+smooth.x*.14-.65*(1-reveal)+Math.sin(time*.22)*.075;
        camera.position.y+=.16*(1-reveal)-smooth.y*.045;
        camera.position.z+=.95*(1-reveal)+Math.sin(time*.17)*.035;
        camera.position.y+=.22*(1-arrival)*stationDirection;
        camera.position.z+=.38*(1-arrival);
        look.x+=shotOffset*.32;
      }
      camera.fov=baseFov+(reduced?0:2.5*(1-reveal));camera.updateProjectionMatrix();
      camera.lookAt(look);
    }
    updateHit();lens.render(scene,camera);
  }
  // Compile every station's materials before starting the visible choreography.
  // Three's compile traverses hidden station groups too, so later switches share
  // the prepared programs without briefly exposing all five sets.
  try {
    await renderer.compileAsync(scene,camera);
    await lens.prepare();
  } catch(error) {
    dispose();renderer.domElement.remove();hitButton.remove();throw error;
  }
  stationStart=screenStart=performance.now();
  if(assembly)assembly.start=stationStart;
  if(document.hidden){
    if(hiddenAt!==null)hiddenAt=performance.now();
    else onVisibility();
  }
  frame=requestAnimationFrame(animate);host.classList.add('model-ready');
  function startFlight(done,t){
    const frozenDisplay=document.createElement('canvas');frozenDisplay.width=1024;frozenDisplay.height=768;
    frozenDisplay.getContext('2d').drawImage(display,0,0);
    flight={start:t,from:camera.position.clone(),aim:look.clone(),display:frozenDisplay,done};
  }
  function dispose(){
    document.removeEventListener('visibilitychange',onVisibility);
    pendingFlight=null;
    clearCarousel();
    disposed=true;cancelAnimationFrame(frame);observer.disconnect();movie.pause();movie.removeAttribute('src');movie.load();
    host.removeEventListener('pointermove',onMove);host.removeEventListener('pointerleave',onLeave);
    model.traverse(o=>{if(o.isMesh){o.geometry.dispose();for(const m of (Array.isArray(o.material)?o.material:[o.material]))m.dispose();}});
    roomModel?.traverse(o=>{if(o.isMesh){o.geometry.dispose();for(const m of [].concat(o.material))m.dispose();}});
    texture.dispose();movieTexture.dispose();poster?.dispose();projectionMask.dispose();projection.geometry.dispose();projection.material.dispose();
    studioEnvironment.dispose();lens.dispose();renderer.dispose();renderer.forceContextLoss();
  }
  return {setMode,dispose,get isEntering(){return entering;},enter(done){
    if(entering)return;entering=true;hitButton.disabled=true;
    if(reduced){done();return;}
    host.classList.add('flying-in');host.closest('.showroom')?.classList.add('entering-3d');
    if(assembly||carousel)pendingFlight={start:performance.now(),done};
    else startFlight(done,performance.now());
  }};
}
