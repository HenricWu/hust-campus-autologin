// Build-time dependency: sharp. Runtime consumes only the generated PNG atlas.
const fs = require('node:fs/promises');
const path = require('node:path');
const sharp = require('sharp');
const root = path.resolve(__dirname, '..');
const directory = path.join(root, 'assets', 'icons');
const sizes = [16,18,20,22,24,28,32,40,48];
const statusSizes = [64,72,80,88,96,112,128,144,160];
const colors = ['737980','24292f','202429','ffffff','a0a5ab','178568','a97624','92989e'];
const aliases = {user:'user-round',lock:'lock-keyhole',network:'monitor',globe:'globe',check:'check',play:'log-in',log:'file-text',refresh:'refresh-cw',pause:'pause',external:'external-link'};
async function raster(svg, size) {
  return (await sharp(Buffer.from(svg),{density:288}).resize(size*4,size*4).png().toBuffer()
    .then(b=>sharp(b).resize(size,size,{kernel:'lanczos3'}).png().toBuffer())).toString('base64');
}
(async()=>{
  const atlas={sizes,status_sizes:statusSizes,glyphs:{},status:{}};
  for(const [name,file] of Object.entries(aliases)){
    const original=await fs.readFile(path.join(directory,'svg',file+'.svg'),'utf8');
    for(const color of colors){
      const svg=original.replaceAll('currentColor','#'+color);
      for(const size of sizes)atlas.glyphs[`${name}_${color}_${size}`]=await raster(svg,size);
    }
  }
  const definitions={online:['circle-check','#178568','#eff8f4'],checking:['refresh-cw','#475569','#f3f4f5'],attention:['circle-alert','#a97624','#fbf6ec'],paused:['circle-pause','#737980','#f3f4f5']};
  const drawings={};
  for(const [name,[file,color,bg]] of Object.entries(definitions)){
    const original=await fs.readFile(path.join(directory,'svg',file+'.svg'),'utf8');
    const body=original.slice(original.indexOf('>')+1,original.lastIndexOf('</svg>'));
    const content=`<circle cx="50" cy="50" r="48" fill="${bg}"/><g transform="translate(26 26) scale(2)" fill="none" stroke="${color}" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round">${body}</g>`;
    drawings[name]=content;
    const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">${content}</svg>`;
    for(const size of statusSizes)atlas.status[`${name}_${size}`]=await raster(svg,size);
  }
  await fs.writeFile(path.join(directory,'raster.json'),JSON.stringify(atlas));
  const statusNames=['online','checking','attention','paused'];
  const strip=`<svg xmlns="http://www.w3.org/2000/svg" width="480" height="110" viewBox="0 0 480 110">${statusNames.map((name,i)=>`<g transform="translate(${10+i*120} 5)">${drawings[name]}</g>`).join('')}</svg>`;
  await fs.writeFile(path.join(root,'docs','status-icons.svg'),strip);
  console.log(JSON.stringify({glyphs:Object.keys(atlas.glyphs).length,status_images:Object.keys(atlas.status).length,supersampling:4}));
})();
