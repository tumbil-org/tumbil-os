const { test, expect } = require('@playwright/test');

const url = process.env.TUMBILOS_TEST_URL || 'http://127.0.0.1:8792/';

function route(path = '') {
  return new URL(path, url).toString();
}

async function waitForDashboard(page) {
  await expect(page.locator('#today-date-nav .date-nav-controls')).toBeVisible();
  await expect(page.locator('#business-date')).not.toHaveText('Loading...');
}

async function resetLocalState(page) {
  await page.evaluate(() => {
    localStorage.removeItem('tumbilos_priority_api_base');
    localStorage.removeItem('tumbilos_priority_api_token');
    localStorage.removeItem('tumbilos_priority_local');
    localStorage.removeItem('tumbilos_priority_actor');
  });
}

async function readJson(page, path) {
  return page.evaluate(async target => {
    const response = await fetch(target, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${target} ${response.status}`);
    return response.json();
  }, path);
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort();
}

function boundedMiddleDate(dates) {
  expect(dates.length, 'need at least three dates for bidirectional date-nav assertions').toBeGreaterThanOrEqual(3);
  return {
    previous: dates[1],
    current: dates[2],
    next: dates[3] || dates[dates.length - 1],
  };
}

function latestArchivedWindow(dates, liveDate) {
  const archived = liveDate ? dates.filter(date => date !== liveDate) : dates;
  expect(archived.length, 'need at least two archived dates for dashboard date-nav assertions').toBeGreaterThanOrEqual(2);
  const current = archived[archived.length - 1];
  const previous = archived[archived.length - 2];
  const next = dates[dates.indexOf(current) + 1] || liveDate;
  return { previous, current, next };
}

function previousCalendarDate(dateString) {
  const [year, month, day] = dateString.split('-').map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
}

async function dateSets(page) {
  const [daily, live, customers, serviceDetails] = await Promise.all([
    readJson(page, 'data.json'),
    readJson(page, 'live.json'),
    readJson(page, 'customers.json'),
    readJson(page, 'service-details.json'),
  ]);

  return {
    liveDate: live?.today?.business_date,
    dashboard: uniqueSorted([
      ...((daily?.history?.days || []).map(day => day.date)),
      live?.today?.business_date,
    ]),
    customer: uniqueSorted((customers?.days || []).map(day => day.date)),
    service: uniqueSorted((serviceDetails?.days || []).map(day => day.date)),
  };
}

async function expectButtonState(locator, enabled) {
  if (enabled) {
    await expect(locator).toBeEnabled();
  } else {
    await expect(locator).toBeDisabled();
  }
}

async function expectDateNavState(page, navId, { back, forward, today }) {
  const nav = page.locator(navId);
  await expect(nav.locator('.date-nav-controls')).toBeVisible();
  await expectButtonState(nav.getByRole('button', { name: 'Back' }), back);
  await expectButtonState(nav.getByRole('button', { name: 'Forward' }), forward);
  await expectButtonState(nav.getByRole('button', { name: 'Today' }), today);
}

async function expectUrlDate(page, date) {
  await expect.poll(() => new URL(page.url()).searchParams.get('date')).toBe(date);
}

test.beforeEach(async ({ page }) => {
  await page.goto(route());
  await resetLocalState(page);
  await page.reload();
  await waitForDashboard(page);
});

test('@critical all primary dashboard views render without uncaught errors', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text());
  });

  await expect(page.getByText('New Customers').first()).toBeVisible();
  await expect(page.getByText('Second-Order Customers').first()).toBeVisible();
  await expect(page.getByText('Habitual Customers').first()).toBeVisible();
  await expect(page.getByText('Brand New')).toHaveCount(0);
  await expect(page.getByText('Returning / Regular')).toHaveCount(0);

  const tabs = ['Overview', 'Acquisition', 'Analyst Brief', 'Priorities', 'App Monitor', 'Data Health'];
  for (const tab of tabs) {
    await page.locator('.tab', { hasText: tab }).click();
    await expect(page.locator('.view.active')).toBeVisible();
  }

  expect(errors).toEqual([]);
});


test('@critical app monitor tab renders live app/API probes', async ({ page }) => {
  await page.locator('.tab', { hasText: 'App Monitor' }).click();
  await expect(page.locator('#app-monitor-view')).toHaveClass(/active/);
  await expect(page.locator('#app-monitor-status')).not.toHaveText('-');
  await expect(page.locator('#app-monitor-summary')).toContainText(/Operational|Degraded|Down|Unknown/);
  await expect(page.locator('#app-monitor-checks .monitor-check')).toHaveCount(14);
  await expect(page.locator('#app-monitor-checks')).toContainText('Customer app Firebase config');
  await expect(page.locator('#app-monitor-checks')).toContainText('Firebase auth SDK');
  await expect(page.locator('#app-monitor-checks')).toContainText('Mobile Google OAuth account selection');
  await expect(page.locator('#app-monitor-checks')).toContainText('External app ping vantage (CA/US)');
  await expect(page.locator('#app-monitor-checks')).toContainText('Server-side app ping vantage');
  await expect(page.locator('#app-monitor-checks')).toContainText('Synthetic login path');
  await expect(page.locator('#app-monitor-checks')).toContainText('Synthetic Google login request validation');
  await expect(page.locator('#app-monitor-checks')).toContainText('Authenticated customer API canary');
  await expect(page.locator('#app-monitor-checks')).toContainText('Authenticated customer app canary');
  await expect(page.locator('#app-monitor-incident-sub')).toContainText(/Was down .* Back up|Last recovered|No active incident/);
  const historyText = await page.locator('#app-monitor-history').innerText();
  expect(historyText).toMatch(/Resolved|down|ok/i);
  if (/Resolved|down/i.test(historyText)) {
    expect(historyText).toMatch(/Down .* Back up|Critical app\/login checks failing/i);
  } else {
    expect(historyText).toMatch(/All customer app, auth, and API checks passed/i);
  }
});

test('@critical date navigation buttons remain correct across dashboard and drilldown views', async ({ page }) => {
  const dates = await dateSets(page);
  const dashboardWindow = boundedMiddleDate(dates.dashboard);
  const customerWindow = boundedMiddleDate(dates.customer);
  const serviceWindow = boundedMiddleDate(dates.service);

  await page.goto(route(`?screen=today&date=${dashboardWindow.current}`));
  await waitForDashboard(page);
  await expectDateNavState(page, '#today-date-nav', { back: true, forward: true, today: true });
  await page.locator('#today-date-nav').getByRole('button', { name: 'Back' }).click();
  await expectUrlDate(page, dashboardWindow.previous);
  await expectDateNavState(page, '#today-date-nav', { back: true, forward: true, today: true });
  await page.locator('#today-date-nav').getByRole('button', { name: 'Forward' }).click();
  await expectUrlDate(page, dashboardWindow.current);

  await page.getByRole('button', { name: 'Acquisition' }).click();
  await expect(page.locator('#acquisition-date-nav').getByText('Historical daily view')).toBeVisible();
  await expectDateNavState(page, '#acquisition-date-nav', { back: true, forward: true, today: true });

  await page.goto(route(`?screen=customer&date=${customerWindow.current}&customerType=brand_new`));
  await expect(page.locator('#customer-screen-title')).toContainText('New Customers');
  await expectDateNavState(page, '#customer-date-nav', { back: true, forward: true, today: true });
  await page.locator('#customer-date-nav').getByRole('button', { name: 'Back' }).click();
  await expectUrlDate(page, customerWindow.previous);
  await expectDateNavState(page, '#customer-date-nav', { back: true, forward: true, today: true });
  await page.goBack();
  await expectUrlDate(page, customerWindow.current);
  await expectDateNavState(page, '#customer-date-nav', { back: true, forward: true, today: true });
  await page.goForward();
  await expectUrlDate(page, customerWindow.previous);
  await expectDateNavState(page, '#customer-date-nav', { back: true, forward: true, today: true });

  await page.goto(route(`?screen=service&date=${serviceWindow.current}&serviceType=tips`));
  await expect(page.locator('#service-screen-title')).toContainText('Tips Received');
  await expectDateNavState(page, '#service-date-nav', { back: true, forward: true, today: true });
  await page.locator('#service-date-nav').getByRole('button', { name: 'Forward' }).click();
  await expectUrlDate(page, serviceWindow.next);
  await expectDateNavState(page, '#service-date-nav', { back: true, forward: true, today: true });
});

test('@critical today-date-nav Back is enabled at the live date even with a non-adjacent prior history day', async ({ page }) => {
  const dates = await dateSets(page);
  const liveDate = dates.liveDate;
  expect(liveDate, 'live date required for today-date-nav assertion').toBeTruthy();
  const dashboardDates = dates.dashboard;
  expect(dashboardDates.length, 'need at least two dashboard dates to exercise Back at live').toBeGreaterThanOrEqual(2);
  expect(dashboardDates[dashboardDates.length - 1]).toBe(liveDate);
  const previousDate = dashboardDates[dashboardDates.length - 2];
  expect(
    previousDate,
    `Back from live date ${liveDate} must land on the prior business date`,
  ).toBe(previousCalendarDate(liveDate));

  await page.goto(route(`?screen=today&date=${liveDate}`));
  await waitForDashboard(page);
  await expectDateNavState(page, '#today-date-nav', { back: true, forward: false, today: false });

  await page.locator('#today-date-nav').getByRole('button', { name: 'Back' }).click();
  await expectUrlDate(page, previousDate);
  await expectDateNavState(page, '#today-date-nav', {
    back: dashboardDates.length >= 3,
    forward: true,
    today: true,
  });
});

test('@critical detail back buttons return to the live overview without breaking browser history', async ({ page }) => {
  const dates = await dateSets(page);
  const latestCustomerDate = dates.customer[dates.customer.length - 1];
  const latestServiceDate = dates.service[dates.service.length - 1];

  await page.goto(route(`?screen=customer&date=${latestCustomerDate}&customerType=brand_new`));
  await expect(page.locator('#customer-screen-title')).toContainText('New Customers');
  await expectDateNavState(page, '#customer-date-nav', { back: true, forward: false, today: latestCustomerDate !== dates.liveDate });
  await page.locator('#customer-back-btn').click();
  await expect(page).toHaveURL(/screen=today/);
  await expect(page.locator('#today-view')).toHaveClass(/active/);

  await page.goBack();
  await expect(page).toHaveURL(/screen=customer/);
  await expect(page.locator('#customer-screen-title')).toContainText('New Customers');

  await page.goto(route(`?screen=service&date=${latestServiceDate}&serviceType=ratings`));
  await expect(page.locator('#service-screen-title')).toContainText('Star Ratings');
  await expectDateNavState(page, '#service-date-nav', { back: true, forward: false, today: latestServiceDate !== dates.liveDate });
  await page.locator('#service-back-btn').click();
  await expect(page).toHaveURL(/screen=today/);
  await expect(page.locator('#today-view')).toHaveClass(/active/);
});

test('@critical Overview KPI tiles bind real data on a within-window historical date', async ({ page }) => {
  const dates = await dateSets(page);
  const dashboardSet = new Set(dates.dashboard);
  const liveDate = dates.liveDate;
  const overlap = dates.service.filter(d => dashboardSet.has(d) && d !== liveDate);
  expect(overlap.length, 'need at least one historical date in both data.json history and the service-detail window').toBeGreaterThanOrEqual(1);
  const target = overlap[overlap.length - 1];

  await page.goto(route(`?screen=today&date=${target}`));
  await waitForDashboard(page);

  await expect(page.locator('#ratings-value'), 'ratings KPI must bind data on within-window historical date').not.toHaveText('-');
  await expect(page.locator('#ratings-sub')).toContainText('ratings');
  await expect(page.locator('#ratings-sub'), 'within-window date must not show snapshot-unavailable message').not.toContainText('unavailable');
  await expect(page.locator('#tips-total')).not.toHaveText('-');
  await expect(page.locator('#tips-sub')).toContainText('tipped orders');
});

test('@critical every today-nav historical date is covered by the service-detail rolling window', async ({ page }) => {
  const dates = await dateSets(page);
  const serviceSet = new Set(dates.service);
  const liveDate = dates.liveDate;
  const historicalDashboardDates = dates.dashboard.filter(d => d !== liveDate);
  const orphans = historicalDashboardDates.filter(d => !serviceSet.has(d));
  const summary = orphans.length > 5
    ? `${orphans.slice(0, 5).join(', ')}, ... (${orphans.length} total)`
    : orphans.join(', ');
  expect(
    orphans,
    `today-nav exposes historical dates with no service-detail payload: ${summary}. Either backfill data.json history or extend the service-detail rolling window so the Overview KPI tiles stop showing the snapshot-unavailable empty state on those dates.`,
  ).toEqual([]);
});

test('@critical customer detail renders GA4 Google Ads source labels from the payload', async ({ page }) => {
  const customers = await readJson(page, 'customers.json');
  let target = null;
  for (const day of customers.days || []) {
    const googleOrder = ((day.customers_by_type || {}).brand_new || [])
      .find(row => (row.source || {}).bucket === 'Google Ads');
    if (googleOrder) {
      target = { date: day.date, order: googleOrder };
    }
  }

  expect(target, 'need at least one retained brand-new customer with GA4 Google Ads attribution').toBeTruthy();
  await page.goto(route(`?screen=customer&date=${target.date}&customerType=brand_new`));
  await expect(page.locator('#customer-screen-title')).toContainText('New Customers');

  const card = page.locator('.customer-card', { hasText: `Order #${Number(target.order.order_id).toLocaleString()}` });
  await expect(card).toBeVisible();
  await expect(card.locator('.confidence', { hasText: 'Google Ads' })).toBeVisible();
  if ((target.order.source || {}).campaign) {
    await expect(card).toContainText(target.order.source.campaign);
  }
});

test('priority board supports rapid local card creation, edit, and drag', async ({ page }) => {
  await page.getByRole('button', { name: 'Priorities' }).click();
  await expect(page.getByRole('button', { name: 'Sync' })).toHaveCount(0);

  const nowInput = page.locator('.quick-add[data-status="IN PROGRESS"] input');
  await nowInput.fill('QA quick card');
  await nowInput.press('Enter');
  await expect(page.getByText('QA quick card')).toBeVisible();
  await expect(page.getByText('Local draft')).toBeVisible();

  await page.locator('.task', { hasText: 'QA quick card' }).locator('[data-edit]').click();
  await page.locator('#task-title').fill('QA edited card');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByText('QA edited card')).toBeVisible();

  const nextLane = page.locator('.lane[data-status="NEW FOR DISCUSSION"]');
  await page.locator('.task', { hasText: 'QA edited card' }).dragTo(nextLane);
  await expect(nextLane.locator('.task', { hasText: 'QA edited card' })).toBeVisible();
});

test('stale old-schema local priority draft does not override priority snapshot', async ({ page }) => {
  const snapshot = await readJson(page, 'priorities.json');
  const snapshotCount = (snapshot.items || []).length;

  await page.evaluate(() => {
    localStorage.setItem('tumbilos_priority_local', JSON.stringify({
      version: 1,
      updated_at: '2026-05-13T08:14:14-04:00',
      columns: ['Now', 'Next', 'Blocked', 'Later', 'Done'],
      areas: ['Product', 'Finance', 'Eng', 'AI Infra'],
      items: [{
        id: 'stale-test-card',
        title: 'test',
        owner: 'Cliff',
        area: 'Product',
        priority: 'P1',
        status: 'Next',
        sort: 1,
      }],
    }));
  });
  await page.reload();
  await page.getByRole('button', { name: 'Priorities' }).click();
  await expect(page.locator('.lane[data-status="BACKLOG"]')).toBeVisible();
  await expect(page.locator('.lane[data-status="Next"]')).toHaveCount(0);
  await expect(page.locator('#priorities-summary')).toContainText(`${snapshotCount} items`);
  await expect(page.getByText('test', { exact: true })).toHaveCount(0);
});

test('@critical Overview KPI cards are grouped into daily / monthly / long-horizon families', async ({ page }) => {
  // Regression for TO-002: the Overview previously rendered every KPI inside a
  // single flat grid, mixing today-to-date cards with month-to-date (Avg
  // Deliveries / Day MTD) and 180-day cohort cards (Revenue / Customer, LTV /
  // Customer). The fix split the grid into three time-horizon groups with their
  // own headings. This test pins the symptom: each card must live inside the
  // correct family wrapper.

  await page.locator('.tab', { hasText: 'Overview' }).click();
  await expect(page.locator('#today-view')).toHaveClass(/active/);

  // Locate the three family headings inside the Overview view. The fix can use
  // any wording for daily / monthly / long-horizon as long as the three groups
  // are visually distinct, so match each with a case-insensitive regex.
  const overview = page.locator('#today-view');
  const dailyHeading = overview.getByText(/daily/i).first();
  const monthlyHeading = overview.getByText(/monthly/i).first();
  const longHorizonHeading = overview.getByText(/long[-\s]?horizon/i).first();

  await expect(dailyHeading, 'Overview must show a Daily group heading').toBeVisible();
  await expect(monthlyHeading, 'Overview must show a Monthly group heading').toBeVisible();
  await expect(longHorizonHeading, 'Overview must show a Long-horizon group heading').toBeVisible();

  // For each heading, walk up the DOM to find the nearest ancestor that contains
  // that heading plus at least one KPI card. That ancestor is the "family
  // wrapper" - the visual grouping the user sees.
  async function familyWrapperFor(headingLocator) {
    return headingLocator.evaluateHandle(node => {
      let el = node;
      while (el && el.parentElement) {
        const parent = el.parentElement;
        // Look for an ancestor that holds both the heading and a card-like child.
        const hasCard = parent.querySelector('.card, .kpi, [class*="kpi"]');
        if (hasCard && parent !== document.body) {
          return parent;
        }
        el = parent;
      }
      return node.parentElement;
    });
  }

  async function cardTextsIn(wrapperHandle) {
    return wrapperHandle.evaluate(node => {
      // Grab the visible text from each KPI card inside this wrapper, where a
      // "card" is any descendant carrying the .card class or a class that looks
      // like a KPI tile. Trim to the card's title so substring matches are clean.
      const candidates = Array.from(node.querySelectorAll('.card, .kpi, [class*="kpi"]'));
      return candidates.map(el => (el.textContent || '').replace(/\s+/g, ' ').trim());
    });
  }

  const dailyWrapper = await familyWrapperFor(dailyHeading);
  const monthlyWrapper = await familyWrapperFor(monthlyHeading);
  const longHorizonWrapper = await familyWrapperFor(longHorizonHeading);

  const dailyTexts = (await cardTextsIn(dailyWrapper)).join(' || ');
  const monthlyTexts = (await cardTextsIn(monthlyWrapper)).join(' || ');
  const longHorizonTexts = (await cardTextsIn(longHorizonWrapper)).join(' || ');

  // The three wrappers must be distinct DOM nodes - otherwise the cards aren't
  // actually grouped, they're all still in one flat grid with three labels.
  const wrapperIdentity = await page.evaluate(([d, m, l]) => {
    return d !== m && m !== l && d !== l;
  }, [dailyWrapper, monthlyWrapper, longHorizonWrapper]);
  expect(wrapperIdentity, 'Daily, Monthly, and Long-horizon families must be distinct DOM containers').toBe(true);

  // Avg Deliveries / Day MTD belongs in the Monthly family, NOT Daily.
  expect(monthlyTexts, 'Avg Deliveries / Day MTD must live inside the Monthly group').toMatch(/Avg Deliveries\s*\/\s*Day MTD/i);
  expect(dailyTexts, 'Avg Deliveries / Day MTD must NOT be wedged inside the Daily group').not.toMatch(/Avg Deliveries\s*\/\s*Day MTD/i);

  // Revenue / Customer and LTV / Customer are 180-day cohort metrics - must be
  // in the Long-horizon family, not the Daily strip.
  expect(longHorizonTexts, 'Revenue / Customer must live inside the Long-horizon group').toMatch(/Revenue\s*\/\s*Customer/i);
  expect(longHorizonTexts, 'LTV / Customer must live inside the Long-horizon group').toMatch(/LTV\s*\/\s*Customer/i);
  expect(dailyTexts, 'Revenue / Customer must NOT sit flush against the Daily cards').not.toMatch(/Revenue\s*\/\s*Customer/i);
  expect(dailyTexts, 'LTV / Customer must NOT sit flush against the Daily cards').not.toMatch(/LTV\s*\/\s*Customer/i);

  // Sanity-check that known today-to-date metrics are still inside the Daily
  // family - otherwise the fix would have moved the wrong cards.
  for (const dailyLabel of ['Placed Today', 'New Customers', 'Delivered', 'Order Value']) {
    expect(dailyTexts, `${dailyLabel} must live inside the Daily group`).toMatch(new RegExp(dailyLabel.replace(/\s+/g, '\\s*'), 'i'));
  }
});

test('@critical priority board fits mobile viewport without page-level horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(route());
  await resetLocalState(page);
  await page.reload();
  await page.getByRole('button', { name: 'Priorities' }).click();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await expect(page.locator('#priority-board')).toBeVisible();
});
