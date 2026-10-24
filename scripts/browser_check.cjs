// Requires Playwright with Edge installed; PLAYWRIGHT_MODULE may point to an existing installation.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('node:fs');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const errors=[];
  const page=await browser.newPage({viewport:{width:1440,height:1100}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8128');
  await page.locator('#selectionStatus').filter({hasText:'初筛结果'}).waitFor();
  await page.locator('#quote').click();
  await page.locator('#quoteResult').filter({hasText:'待供应商询价'}).waitFor();
  if((await page.locator('#quoteResult').innerText()).includes('AUD 0'))throw Error('Missing price shown as zero');
  await page.locator('#phase').selectOption('unknown');
  await page.locator('#select').click();
  await page.locator('#selectionStatus').filter({hasText:'需要补充信息'}).waitFor();
  if(await page.locator('#candidates button').count())throw Error('Unknown phase must request clarification');
  await page.locator('#phase').selectOption('three');
  await page.locator('#brand').selectOption('HYXiPOWER');
  await page.locator('#capacity').fill('20');await page.locator('#power').fill('5');
  await page.locator('#select').click();
  await page.locator('#candidates').filter({hasText:'HYXI E200 + HYX-H10K-HT'}).waitFor();
  await page.locator('#quote').click();
  const offerInWindow=new Date().toISOString().slice(0,10)<'2026-11-01';
  await page.locator('#quoteResult').filter({hasText:offerInWindow?'AUD 3157.00（公开条件价）':'待供应商询价'}).waitFor();
  if(offerInWindow&&!(await page.locator('#quoteResult').innerText()).includes('完整总价尚未确定'))throw Error('Conditional price presented as final');
  if(!await page.locator('#quoteResult a[href="https://www.ozbargain.com.au/node/977206"]').count())throw Error('Public price source missing');
  fs.mkdirSync(path.join(root,'docs/images'),{recursive:true});
  await page.locator('#quoteResult').screenshot({path:path.join(root,'docs/images/quote.png')});
  await page.locator('#brand').selectOption('any');
  await page.locator('#capacity').fill('30');await page.locator('#power').fill('12');
  await page.locator('#select').click();
  await page.locator('#selectionStatus').filter({hasText:'1 个初筛结果'}).waitFor();
  await page.locator('#candidates').filter({hasText:'15 kW / 36 kWh'}).waitFor();
  if(!(await page.locator('#candidates').innerText()).includes('15 kW / 36 kWh'))throw Error('Incorrect three-phase match');
  await page.locator('#sample').click();await page.locator('#research').click();
  await page.waitForFunction(()=>document.querySelector('#runStatus').textContent==='待人工确认',null,{timeout:180000});
  const report=await page.locator('#report').innerText();
  const trace=await page.locator('#trace').innerText();
  if(!report.includes('KH')||!trace.includes('研究结束'))throw Error('Missing research or SSE final event');
  if(!await page.locator('#evidence a[href*="papower.com.au"]').count())throw Error('Source links missing');
  const desktopOverflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
  fs.mkdirSync(path.join(root,'docs/images'),{recursive:true});
  await page.evaluate(()=>scrollTo(0,0));
  await page.screenshot({path:path.join(root,'docs/images/desktop.png')});
  const identityDetails=page.locator('details').filter({has:page.locator('#role')});
  await identityDetails.evaluate(el=>el.open=true);
  await page.locator('#role').selectOption('viewer');await page.locator('#login').click();
  await page.locator('#error').filter({hasText:'无权'}).waitFor();
  if((await page.locator('#report').innerText())===report||await page.locator('#evidence .evidence').count())throw Error('Role switch retained prior protected report');
  await page.locator('#role').selectOption('manager');await page.locator('#login').click();
  await page.getByLabel('人工核对意见').fill('页面联调：已核对来源与待核实项，仅确认内部草稿。');
  await page.getByRole('button',{name:'确认草稿',exact:true}).click();
  await page.locator('#runStatus').filter({hasText:'草稿已确认'}).waitFor();
  await page.locator('#report').scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(root,'docs/images/research.png')});
  const mobile=await browser.newPage({viewport:{width:390,height:844},isMobile:true,deviceScaleFactor:1});
  mobile.on('pageerror',e=>errors.push(e.message));
  await mobile.goto('http://127.0.0.1:8128');
  await mobile.locator('#selectionStatus').filter({hasText:'初筛结果'}).waitFor();
  const mobileOverflow=await mobile.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
  await mobile.screenshot({path:path.join(root,'docs/images/mobile.png')});
  const today=new Date().toLocaleDateString('en-CA',{timeZone:'Australia/Sydney'});
  const result={checked_on:today,desktop_viewport:[1440,1100],mobile_viewport:[390,844],
    quote_missing_price:'null, awaiting supplier price',unknown_phase:'clarification, no recommendation',
    conditional_public_offer:offerInWindow?'AUD 3157.00, final total unverified':'installation window ended, no active amount',
    three_phase_30kwh_12kw:'SigenStor 15kW / 36kWh',source_links:true,sse_final_event:true,
    role_switch_clears_protected_content:true,manager_review:true,desktop_overflow:desktopOverflow,
    mobile_overflow:mobileOverflow,page_errors:errors};
  fs.writeFileSync(path.join(root,'results/battery_browser_'+today.replaceAll('-','')+'.json'),JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify(result));
  if(errors.length||desktopOverflow||mobileOverflow)throw Error('Browser QA failed');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
