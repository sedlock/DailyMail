// DISPOSABLE RECON PROBE - maps URL query params -> ActionGetHomeData inputParameters.
const { chromium } = require('playwright');
const BASE='https://apps.rowan.edu/RowanAnnouncer';
(async()=>{
  const browser=await chromium.launch();
  const urls=[
    `${BASE}/Home`,
    `${BASE}/Home?Audience=Employees&CurrentDate=2026-08-20`,
    `${BASE}/Home?Audience=Students&CurrentDate=2026-08-20`,
    `${BASE}/Home?Audience=Employees&CurrentDate=2026-08-20&Category=5`,
    `${BASE}/Home?Audience=Employees&CurrentDate=1999-01-01`,
    `${BASE}/Home?Audience=Bogus`,
    `${BASE}/Home?Audience=Employees&CurrentDate=not-a-date`,
  ];
  for(const url of urls){
    const ctx=await browser.newContext({ignoreHTTPSErrors:true});
    const page=await ctx.newPage();
    let sent=null, got=null;
    page.on('request',r=>{ if(r.url().includes('ActionGetHomeData')) sent=r.postData(); });
    page.on('response',async r=>{ if(r.url().includes('ActionGetHomeData')){ try{ const j=JSON.parse(await r.text()); got={total:j.data&&j.data.TotalCount, n:(j.data&&j.data.Announcements?j.data.Announcements.List.length:null), apiChg:j.versionInfo&&j.versionInfo.hasApiVersionChanged}; }catch(e){} }});
    await page.goto(url,{waitUntil:'networkidle',timeout:90000}).catch(()=>{});
    await page.waitForTimeout(3500);
    const hdr = await page.evaluate(()=>{const el=document.querySelector('h1,h2,h3'); return document.body.innerText.split('\n').filter(l=>/Announcements -/.test(l))[0]||'';}).catch(()=>'');
    console.log(`\nURL: ${url.replace(BASE,'')}`);
    console.log(`  header line : ${hdr}`);
    if(sent){ const j=JSON.parse(sent); console.log(`  -> inputParameters: ${JSON.stringify(j.inputParameters)}`);}
    else console.log('  -> NO ActionGetHomeData call');
    console.log(`  <- ${JSON.stringify(got)}`);
    await ctx.close();
  }
  await browser.close();
})();
