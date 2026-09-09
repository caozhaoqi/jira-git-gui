"""验证侧栏 tab 标签：确认 tab.kibana raw key 已消失、Kibana 日志 出现。"""
from playwright.sync_api import sync_playwright

URLS = [
    ('/', 'zh-default'),
    ('/web/', 'web-zh'),
    # 其余 locale 不重复验（i18n key 是三语同时加的，逻辑一致）
]

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    )
    ctx = browser.new_context(viewport={'width': 1366, 'height': 800})
    page = ctx.new_page()

    for url, label in URLS:
        page.goto(f'http://127.0.0.1:8787{url}', wait_until='domcontentloaded')
        page.wait_for_selector('.tabs .tab', timeout=5000)
        # 抓每个 tab 的 data-tab + 显示文字
        rows = page.evaluate("""
            () => Array.from(document.querySelectorAll('.tabs .tab')).map(el => ({
                dataTab: el.getAttribute('data-tab'),
                text: (el.querySelector('.tab-txt')?.textContent || '').trim(),
            }))
        """)
        kibana_row = next((r for r in rows if r['dataTab'] == 'kibana'), None)
        print(f'[{label}] {url}')
        print(f'  tabs 共 {len(rows)} 条')
        for r in rows:
            mark = ' <<<' if r['text'].startswith('tab.') else ''
            print(f"    {r['dataTab']:<10} -> '{r['text']}'{mark}")
        assert kibana_row, f'Kibana tab 没渲染 ({label})'
        assert not kibana_row['text'].startswith('tab.'), \
            f'Kibana 仍显示 raw key: {kibana_row["text"]}'
        assert kibana_row['text'] in ('Kibana 日志', 'Kibana', 'Kibana ログ'), \
            f'Kibana 文案异常: {kibana_row["text"]}'
        print(f'  ✅ Kibana tab 文字 = "{kibana_row["text"]}"')

    # 顺带验证其它 10 个 tab 都不是 raw key
    rows = page.evaluate("""
        () => Array.from(document.querySelectorAll('.tabs .tab')).map(el => ({
            dataTab: el.getAttribute('data-tab'),
            text: (el.querySelector('.tab-txt')?.textContent || '').trim(),
        }))
    """)
    bad = [r for r in rows if r['text'].startswith('tab.') or r['text'].startswith('kibana.')]
    assert not bad, f'仍存在 raw key: {bad}'
    print('\n✅ 全 11 个 tab 都有正确本地化文案，无 raw key 残留')

    browser.close()