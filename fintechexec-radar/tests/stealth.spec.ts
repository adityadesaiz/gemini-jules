import { test } from '@playwright/test';
import { chromium } from 'playwright-extra';
import stealth from 'puppeteer-extra-plugin-stealth';

chromium.use(stealth());

test('stealth browser launches and takes screenshot', async () => {
  // Launch a stealthy Chromium browser in headless mode for CI/automation
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  // Navigate to the Next.js development server root
  // Using a direct URL here, as the custom browser instance doesn't use baseURL from config directly
  await page.goto('http://localhost:3000');

  // Take a screenshot to verify the page loaded correctly
  await page.screenshot({ path: 'fintechexec-radar/stealth_test.png' });

  // Close the browser
  await browser.close();
});
