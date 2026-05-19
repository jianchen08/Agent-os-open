/**
 * 诊断脚本 - 检查侧边栏可见性问题
 */
import { chromium } from 'playwright';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  // Login
  await page.goto('http://127.0.0.1:5188/login', { waitUntil: 'networkidle' });
  await page.waitForTimeout(500);
  if (page.url().includes('/login')) {
    await page.locator('#username').fill('admin');
    await page.locator('#password').fill('admin123');
    await page.locator('button[type="submit"]').click();
    await page.waitForTimeout(2000);
  }

  // Navigate to home
  await page.goto('http://127.0.0.1:5188/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // 1. Check layout mode
  const layoutMode = await page.evaluate(() => {
    try {
      const data = JSON.parse(localStorage.getItem('layout-mode-storage') || '{}');
      return data?.state?.mode || 'unknown';
    } catch { return 'error'; }
  });
  console.log('Layout mode:', layoutMode);

  // 2. Check aside elements
  const asideInfo = await page.evaluate(() => {
    const asides = document.querySelectorAll('aside');
    const results = [];
    for (const aside of asides) {
      const rect = aside.getBoundingClientRect();
      const style = window.getComputedStyle(aside);
      const children = aside.querySelectorAll('*');
      results.push({
        width: rect.width,
        height: rect.height,
        top: rect.top,
        left: rect.left,
        display: style.display,
        overflow: style.overflow,
        visibility: style.visibility,
        opacity: style.opacity,
        childCount: children.length,
        firstChildTag: aside.firstElementChild?.tagName,
      });
    }
    return results;
  });
  console.log('Aside elements:', JSON.stringify(asideInfo, null, 2));

  // 3. Check session items
  const sessionInfo = await page.evaluate(() => {
    const groups = document.querySelectorAll('aside .group');
    return Array.from(groups).slice(0, 3).map(g => {
      const rect = g.getBoundingClientRect();
      const style = window.getComputedStyle(g);
      const titleDiv = g.querySelector('div[class*="cursor-pointer"]');
      const titleRect = titleDiv ? titleDiv.getBoundingClientRect() : null;
      const titleStyle = titleDiv ? window.getComputedStyle(titleDiv) : null;
      return {
        text: (g.textContent || '').trim().substring(0, 30),
        width: rect.width,
        height: rect.height,
        top: rect.top,
        left: rect.left,
        display: style.display,
        visibility: style.visibility,
        overflow: style.overflow,
        titleDivRect: titleRect ? { w: titleRect.width, h: titleRect.height, t: titleRect.top, l: titleRect.left } : null,
        titleDivDisplay: titleStyle?.display,
        titleDivVisibility: titleStyle?.visibility,
      };
    });
  });
  console.log('Session items:', JSON.stringify(sessionInfo, null, 2));

  // 4. Check new session button
  const newSessionInfo = await page.evaluate(() => {
    const btn = document.querySelector('button');
    const allBtns = document.querySelectorAll('aside button, button');
    let newSessionBtn = null;
    for (const b of allBtns) {
      if ((b.textContent || '').includes('新会话')) {
        newSessionBtn = b;
        break;
      }
    }
    if (!newSessionBtn) return { found: false, totalButtons: allBtns.length };

    const rect = newSessionBtn.getBoundingClientRect();
    const style = window.getComputedStyle(newSessionBtn);
    return {
      found: true,
      totalButtons: allBtns.length,
      width: rect.width,
      height: rect.height,
      top: rect.top,
      left: rect.left,
      display: style.display,
      visibility: style.visibility,
      opacity: style.opacity,
      text: (newSessionBtn.textContent || '').trim(),
    };
  });
  console.log('New session button:', JSON.stringify(newSessionInfo, null, 2));

  // 5. Check Playwright's visibility
  const aside = page.locator('aside').first();
  console.log('Playwright aside count:', await aside.count());
  console.log('Playwright aside isVisible:', await aside.isVisible().catch(() => 'error'));
  console.log('Playwright aside boundingBox:', await aside.boundingBox());

  // 6. Try clicking with force
  const firstSession = page.locator('aside .group').first();
  console.log('First session count:', await firstSession.count());
  console.log('First session boundingBox:', await firstSession.boundingBox());

  if (await firstSession.count() > 0) {
    try {
      await firstSession.click({ force: true });
      await page.waitForTimeout(2000);
      console.log('Force click succeeded!');
      console.log('URL after click:', page.url());

      // Check if chat area loaded
      const textarea = page.locator('textarea').first();
      console.log('Textarea count after click:', await textarea.count());
      console.log('Textarea visible:', await textarea.isVisible().catch(() => false));
    } catch (e) {
      console.log('Force click failed:', e.message);
    }
  }

  await page.screenshot({ path: 'e2e-deep-screenshots/diagnostic.png' });
  await browser.close();
})();
