// DISPOSABLE RECON PROBE - UI zero-state and pagination/lazy-load behaviour.
const { chromium } = require('playwright');
const fs=require('fs');
const OUT='/home/sedlock/src/DailyMail/artifacts/reconnaissance';
const BASE='https://apps.rowan.edu/RowanAnnouncer';
(async()=>{
  const browser=await chromium.launch();
  const cases=[
    ['zero-state','Home?Audience=Employees&CurrentDate=1999-01-01'],
    ['high-count','Home?Audience=Employees&CurrentDate=2025-03-05'],
  ];
  for(const [label,path] of cases){
    const ctx=await browser.newContext({ignoreHTTPSErrors:true});
    const page=await ctx.newPage();
    const calls=[];
    page.on('request',r=>{ if(r.url().includes('ActionGetHomeData')){ const j=JSON.parse(r.postData()); calls.push(j.inputParameters); }});
    await page.goto(`${BASE}/${path}`,{waitUntil:'networkidle',timeout:90000}).catch(()=>{});
    await page.waitForTimeout(3500);
    const before=calls.length;
    // scroll to bottom repeatedly to trigger any lazy loading
    for(let i=0;i<6;i++){ await page.evaluate(()=>window.scrollTo(0,document.body.scrollHeight)); await page.waitForTimeout(1500); }
    const txt=await page.evaluate(()=>document.body.innerText);
    await page.screenshot({path:`${OUT}/screenshots/${label}.png`,fullPage:false});
    // count rendered announcement cards heuristically
    const cards=await page.evaluate(()=>document.querySelectorAll('a[href*="SubmissionId="]').length);
    const links=await page.evaluate(()=>[...new Set([...document.querySelectorAll('a[href*="SubmissionId="]')].map(a=>a.getAttribute('href')))]);
    console.log(`\n##### ${label} (${path}) #####`);
    console.log(`ActionGetHomeData calls: initial=${before} afterScroll=${calls.length}`);
    calls.forEach((c,i)=>console.log(`   call${i}: StartIndex=${c.StartIndex} MaxRecords=${c.MaxRecords} Audience=${c.Filters.Audience} StartDate=${c.Filters.StartDate}`));
    console.log(`rendered SubmissionId links: ${cards} (unique ${links.length})`);
    console.log(`sample hrefs: ${links.slice(0,3).join(' , ')}`);
    // look for load-more / pagination controls & empty-state message
    const kw=['No announcements','no announcements','Load More','Show More','Next','There are no'];
    for(const k of kw) if(txt.includes(k)) console.log(`  TEXT MATCH: ${JSON.stringify(k)}`);
    console.log(`--- text tail (400) ---\n${txt.slice(-400)}`);
    fs.writeFileSync(`${OUT}/html/ui-${label}.txt`,txt);
    await ctx.close();
  }
  await browser.close();
})();
