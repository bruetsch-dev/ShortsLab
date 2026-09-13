// Fine physical variation in object space: it follows the furniture during entry.
// Values stay restrained so the authored base colours and metalness remain intact.
export function finishMaterials(root){
  const done=new Set();
  root.traverse(object=>{
    if(!object.isMesh)return;
    if(object.name.startsWith('Maple_grain')||object.name.startsWith('Maple grain'))object.visible=false;
    for(const material of [].concat(object.material)){
      if(done.has(material)||!material.isMeshStandardMaterial)continue;
      done.add(material);
      const wood=/hardwood|walnut|smoked oak/i.test(material.name);
      const painted=/case|ABS|enamel|trim/i.test(material.name);
      const textile=/upholstery|woven floor/i.test(material.name);
      if(!wood&&!painted&&!textile)continue;
      material.onBeforeCompile=shader=>{
        shader.vertexShader='varying vec3 finishPosition;\n'+shader.vertexShader;
        shader.vertexShader=shader.vertexShader.replace('#include <begin_vertex>','#include <begin_vertex>\nfinishPosition=position;');
        shader.fragmentShader=`varying vec3 finishPosition;
        float surfaceHash(vec3 p){return fract(sin(dot(p,vec3(127.1,311.7,74.7)))*43758.5453);}
        float grainNoise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
          return mix(mix(surfaceHash(vec3(i,0.)),surfaceHash(vec3(i+vec2(1,0),0.)),f.x),mix(surfaceHash(vec3(i+vec2(0,1),0.)),surfaceHash(vec3(i+vec2(1,1),0.)),f.x),f.y);}
        `+shader.fragmentShader;
        shader.fragmentShader=shader.fragmentShader.replace('#include <color_fragment>',`#include <color_fragment>
          float finishNoise=surfaceHash(floor(finishPosition*850.));
          ${wood?`float grain=grainNoise(finishPosition.xz*vec2(75.,2.5));
          grain+=.4*grainNoise(finishPosition.xz*vec2(190.,5.));
          diffuseColor.rgb*=.94+.06*grain;`:textile?'diffuseColor.rgb*=.91+.09*finishNoise;':'diffuseColor.rgb*=.975+.025*finishNoise;'}
        `);
        shader.fragmentShader=shader.fragmentShader.replace('#include <roughnessmap_fragment>',`#include <roughnessmap_fragment>\nroughnessFactor=clamp(roughnessFactor+(finishNoise-.5)*${wood?'.14':'.075'},.05,1.);`);
      };
      material.customProgramCacheKey=()=>wood?'wood-finish-v2':textile?'textile-finish-v2':'paint-finish-v2';
      material.needsUpdate=true;
    }
  });
}
