const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text()); });

  await page.goto('file:///Users/mac/demo-project/tabletalk/tmp-kb-review-v3.html');
  await page.waitForTimeout(700);
  await page.screenshot({ path: '/tmp/kb-v3-1-default.png' });

  // 展开第一张表卡片（orders）
  await page.locator('.tbl-card').first().locator('.tbl-head').click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: '/tmp/kb-v3-2-expanded.png' });

  // 点「商品」标签筛选
  await page.locator('.tc-item', { hasText: '商品' }).locator('.tc-name').click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: '/tmp/kb-v3-3-tagfilter.png' });

  // 关系列筛选：只显示 AI 发现
  await page.click('#srcChips .fchip[data-v="ai"]');
  await page.waitForTimeout(400);
  await page.screenshot({ path: '/tmp/kb-v3-4-relfilter.png' });

  // 打开标签编辑面板
  await page.locator('.tc-item', { hasText: '订单' }).locator('.tc-more').click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: '/tmp/kb-v3-5-tagedit.png' });

  // 切换到「其他」
  await page.locator('.tc-item.other').click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: '/tmp/kb-v3-6-other.png' });

  console.log('ERRORS:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
})();
