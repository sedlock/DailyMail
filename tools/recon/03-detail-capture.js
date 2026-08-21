// DISPOSABLE RECON PROBE - not production code.
const { chromium } = require('playwright');
const fs = require('fs');
const OUT='/home/sedlock/src/DailyMail/artifacts/reconnaissance';
const BASE='https://apps.rowan.edu/RowanAnnouncer';
(async()=>{
  const browser=await chromium.launch();
  const ids=process.argv.slice(2);
  for (const id of ids){
    for (const screen of ['Announcement','Announcement_Details']){
      const ctx=await browser.newContext({ignoreHTTPSErrors:true});
      const page=await ctx.newPage();
      const ev=[];
      page.on('request',r=>{ if(['xhr','fetch'].includes(r.resourceType())) ev.push({p:'req',m:r.method(),u:r.url(),body:r.postData()||null, csrf:r.headers()['x-csrftoken']||null}); });
      page.on('response',async r=>{ if((r.headers()['content-type']||'').includes('json')){ let b=null; try{b=await r.text();}catch(e){} ev.push({p:'res',s:r.status(),u:r.url(),body:b}); }});
      const url=`${BASE}/${screen}?SubmissionId=${id}`;
      const resp=await page.goto(url,{waitUntil:'networkidle',timeout:90000}).catch(e=>null);
      await page.waitForTimeout(4000);
      const txt=await page.evaluate(()=>document.body.innerText).catch(()=>'');
      const label=`${screen}-${id}`;
      fs.writeFileSync(`${OUT}/network/detail-${label}.json`,JSON.stringify(ev,null,2));
      fs.writeFileSync(`${OUT}/html/detail-${label}.txt`,txt);
      console.log(`\n##### ${screen} SubmissionId=${id} -> ${resp?resp.status():'ERR'} #####`);
      console.log('screenservice calls:');
      for(const e of ev) if(e.p==='req'&&e.u.includes('screenservices')) console.log('   ',e.m,e.u.replace(BASE,''),'\n      body:',(e.body||'').slice(0,260));
      console.log('--- visible text (900) ---\n'+txt.slice(0,900));
      await ctx.close();
    }
  }
  await browser.close();
})();
