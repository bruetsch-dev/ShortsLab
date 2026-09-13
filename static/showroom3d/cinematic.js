import * as THREE from './vendor/three.module.js';

// A restrained lens pass: highlights bloom locally, the room falls off at the
// edges. Typography is composed above this canvas and stays sharp.
export function createLens(renderer) {
  const target = new THREE.WebGLRenderTarget(1,1,{type:THREE.HalfFloatType,depthBuffer:true});
  target.samples=4;
  target.depthTexture=new THREE.DepthTexture(1,1,THREE.UnsignedIntType);
  const focalPoint=new THREE.Vector3();
  const uniforms={frame:{value:target.texture},depthFrame:{value:target.depthTexture},pixel:{value:new THREE.Vector2(1,1)},focus:{value:0},nearPlane:{value:.035},farPlane:{value:50},focalDistance:{value:7}};
  const material=new THREE.ShaderMaterial({uniforms,depthTest:false,depthWrite:false,
    vertexShader:`varying vec2 vUv; void main(){vUv=uv;gl_Position=vec4(position.xy,0.,1.);}`,
    fragmentShader:`
      uniform sampler2D frame; uniform sampler2D depthFrame;
      uniform vec2 pixel; uniform float focus;
      uniform float nearPlane; uniform float farPlane; uniform float focalDistance;
      varying vec2 vUv;
      void main(){
        vec3 base=texture2D(frame,vUv).rgb;
        vec3 glow=vec3(0.);
        for(int i=0;i<8;i++){
          float a=float(i)*.785398;
          vec2 offset=vec2(cos(a)*2.8,sin(a)*.7)*pixel*5.;
          glow+=max(texture2D(frame,vUv+offset).rgb-vec3(.85),vec3(0.));
        }
        float depth=texture2D(depthFrame,vUv).x;
        float distanceToCamera=nearPlane*farPlane/(farPlane-depth*(farPlane-nearPlane));
        // A generous focus zone keeps the keyboard and props readable. Only
        // surfaces outside that zone soften; DOM text never enters this pass.
        float separation=max(0.,abs(distanceToCamera-focalDistance)-1.15);
        float depthBlur=clamp(separation/max(focalDistance,.1)*5.,0.,2.6);
        depthBlur*=smoothstep(.5,2.,focalDistance);
        float radius=max(depthBlur,focus*9.);
        if(radius>.08){
          vec3 soft=base*2.;
          for(int i=0;i<8;i++){
            float a=float(i)*.785398;
            soft+=texture2D(frame,vUv+vec2(cos(a),sin(a))*pixel*radius).rgb;
          }
          base=mix(base,soft*.1,max(focus*.72,clamp(depthBlur*.4,0.,.85)));
        }
        vec2 p=vUv-.5;
        float vignette=1.-.26*smoothstep(.08,.55,dot(p,p));
        float luminance=dot(base,vec3(.2126,.7152,.0722));
        vec3 grade=mix(vec3(.90,.96,1.035),vec3(1.025,1.01,.98),smoothstep(.02,.65,luminance));
        gl_FragColor=vec4((base*grade+glow*.022)*vignette,1.);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }`});
  const screen=new THREE.Scene(),camera=new THREE.Camera();
  const geometry=new THREE.PlaneGeometry(2,2);screen.add(new THREE.Mesh(geometry,material));
  return {
    prepare(){return renderer.compileAsync(screen,camera);},
    focus(amount){uniforms.focus.value=Math.max(0,Math.min(1,amount));},
    resize(w,h){const ratio=renderer.getPixelRatio();target.setSize(Math.round(w*ratio),Math.round(h*ratio));uniforms.pixel.value.set(1/(w*ratio),1/(h*ratio));},
    render(scene,view){
      renderer.setRenderTarget(target);renderer.render(scene,view);
      focalPoint.set(0,1.872,.18).applyMatrix4(view.matrixWorldInverse);
      uniforms.focalDistance.value=Math.max(view.near,-focalPoint.z);
      uniforms.nearPlane.value=view.near;uniforms.farPlane.value=view.far;
      renderer.setRenderTarget(null);renderer.render(screen,camera);
    },
    dispose(){target.dispose();geometry.dispose();material.dispose();}
  };
}

export const ease = x => {x=Math.max(0,Math.min(1,x));return x*x*x*(x*(x*6-15)+10);};

export function flightPosition(out, from, t) {
  const k=ease(t), a=1-k;
  // A curved approach first faces the monitor, then travels into its glass.
  return out.set(
    a*a*a*from.x+3*a*a*k*from.x*.65,
    a*a*a*from.y+3*a*a*k*2.16+3*a*k*k*1.872+k*k*k*1.872,
    a*a*a*from.z+3*a*a*k*from.z*.55+3*a*k*k*1.15+k*k*k*.31
  );
}
