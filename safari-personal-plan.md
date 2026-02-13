# Safari + Browser Use: Personal Project Plan

**Total: ~38 SP · ~3-4 weeks of evening/weekend work · Solo**

## How this plan works

Browser Use's architecture is event-driven: the Agent dispatches events like `NavigateToUrlEvent`, `ClickElementEvent`, `TypeTextEvent` through a `bubus` event bus. Watchdogs listen and execute them via CDP. Your job is to build a `SafariBrowserSession` that handles those same events using **safaridriver (Selenium) + JavaScript injection + AppleScript** instead of CDP.

You don't replace every watchdog. You replace the **transport layer** underneath them.

## What you're explicitly NOT building

These features account for ~150 SP in the enterprise plan and are unnecessary for personal use:

- **Network interception / MITM proxy** — Skip entirely. Most agent tasks don't need `Fetch.requestPaused`. Use Safari's built-in content blockers for ad filtering.
- **Headless mode / virtual display** — Safari doesn't support headless. You're running this on your Mac; a visible window is fine.
- **CI/cloud orchestration** — You're running locally.
- **HAR recording** — Nice-to-have, skip for now.
- **Video recording** — Skip.
- **Beta rollout / soak testing / GA packaging** — It's your project, not a product.
- **Cross-origin iframe deep support** — Handle simple iframes; skip the complex OOPIF target routing that CDP enables. If an agent task needs deep iframe traversal, fall back to Chromium for that task.

## What you DO get

When done, you'll be able to run:
```python
from browser_use import Agent
from safari_session import SafariBrowserSession

browser = SafariBrowserSession()
agent = Agent(task="Find the cheapest flight to Tokyo on Google Flights", browser=browser, llm=llm)
await agent.run()
```

And Safari opens on your Mac, navigates, clicks, types, scrolls — driven by the LLM. With your real Safari cookies, extensions, and iCloud Keychain. No anti-bot detection because it IS Safari.

---

## Phase 1: Foundation (10 SP, ~1 week)

### 1.1 — Safari control layer (5 SP)
Build `safari_driver.py`: a thin async wrapper around Selenium's Safari WebDriver.

```
safari_session/
├── __init__.py
├── driver.py          # Async Selenium Safari wrapper
├── dom_extractor.py   # JS injection for DOM state
├── session.py         # SafariBrowserSession (main class)
└── applescript.py     # AppleScript helpers
```

**`driver.py` must expose:**
- `navigate(url)` → `driver.get(url)`
- `screenshot()` → `driver.get_screenshot_as_base64()`
- `execute_js(expression)` → `driver.execute_script()`
- `click_at(x, y)` → ActionChains move + click
- `type_into(selector, text)` → find_element + send_keys
- `scroll(direction, pixels)` → JS `window.scrollBy()`
- `get_url()` / `get_title()`
- `get_cookies()` / `set_cookie()`
- `switch_tab(index)` / `new_tab(url)` / `close_tab()`
- `handle_dialog(accept)`

**Done when:** You can script Safari through Python — navigate, click, type, screenshot — without Browser Use involved.

### 1.2 — DOM extractor via JS injection (5 SP)
This is the hardest single piece. Browser Use's CDP path uses `DOMSnapshot.captureSnapshot` + accessibility tree to build an `EnhancedDOMTreeNode` tree with element indices, positions, attributes, and visibility. You need to replicate this output via injected JavaScript.

**Key insight:** Browser Use already has JS that runs in-page for DOM processing (look in `browser_use/dom/`). Much of it can be reused — it's just JavaScript that runs in any browser. The Safari-specific part is *getting it in there* (via `execute_script`) and *getting structured data out* (via return value).

Your JS extraction script needs to return, for each interactive element:
- `backend_node_id` → use a sequential index as surrogate
- `tag_name`, `text_content`, `attributes` (id, class, aria-label, href, type, placeholder, name, value, role)
- `bounding_rect` (x, y, width, height from `getBoundingClientRect()`)
- `is_visible` (computed from rect, opacity, display, visibility)
- `is_scrollable`

**Done when:** `dom_extractor.extract(driver)` returns a list of element dicts that could plausibly populate a `SerializedDOMState`.

---

## Phase 2: Browser Use Integration (15 SP, ~1.5 weeks)

### 2.1 — SafariBrowserSession class (8 SP)
This is the main adapter. It needs to implement the same interface that Browser Use's `Agent` expects from a `BrowserSession`.

**Strategy: event bus adapter.** Browser Use's agent dispatches events, and watchdogs handle them. You have two options:

**Option A (recommended): Subclass and override.** Fork `session.py`, create `SafariBrowserSession` that inherits from a minimal base, registers its own event handlers for the ~15 core events, and skips the CDP connection entirely.

**Option B: Shim at the watchdog level.** Replace `default_action_watchdog.py` and `screenshot_watchdog.py` with Safari-aware versions. More surgical but more fragile across Browser Use updates.

**The core events you must handle:**

| Event | Safari Implementation |
|---|---|
| `NavigateToUrlEvent` | `driver.get(url)` or `driver.execute_script("window.open(url)")` for new_tab |
| `ClickElementEvent` | Find by index → click via ActionChains at element center coords |
| `ClickCoordinateEvent` | ActionChains move_by_offset + click |
| `TypeTextEvent` | Find element → clear → send_keys |
| `ScrollEvent` | `window.scrollBy()` via JS |
| `ScrollToTextEvent` | JS: find text node, scrollIntoView() |
| `ScreenshotEvent` | `driver.get_screenshot_as_base64()` |
| `BrowserStateRequestEvent` | Run DOM extractor + screenshot + build `BrowserStateSummary` |
| `SwitchTabEvent` | `driver.switch_to.window(handles[i])` |
| `CloseTabEvent` | `driver.close()` + switch |
| `GoBackEvent` | `driver.back()` |
| `GoForwardEvent` | `driver.forward()` |
| `RefreshEvent` | `driver.refresh()` |
| `WaitEvent` | `asyncio.sleep(seconds)` |
| `SendKeysEvent` | ActionChains key combos |
| `GetDropdownOptionsEvent` | JS: query `<option>` elements within `<select>` |
| `SelectDropdownOptionEvent` | Selenium Select helper |
| `UploadFileEvent` | `element.send_keys(file_path)` on file input |

**Done when:** `SafariBrowserSession` can be passed to `Agent()` and the agent loop runs without import errors or immediate crashes.

### 2.2 — DOM state bridge (5 SP)
Map your JS extraction output into Browser Use's `SerializedDOMState` and `EnhancedDOMTreeNode` structures. This is the glue between your `dom_extractor.py` and what the Agent/LLM expects to see.

The LLM receives a text representation of interactive elements like:
```
[1] <input type="text" placeholder="Search" />
[2] <button>Submit</button>
[3] <a href="/about">About Us</a>
```

Your DOM extractor needs to produce data that serializes to this same format.

**Done when:** The agent's DOM state representation for a Safari-loaded page is comparable in quality to the Chromium version. Test on 3-4 common sites (Google, Wikipedia, GitHub, Amazon).

### 2.3 — Dialog and popup handling (2 SP)
Wire up dialog detection. Safaridriver supports `driver.switch_to.alert` for basic alert/confirm/prompt. For `onbeforeunload`, use a polling approach: attempt to detect and dismiss after navigation actions.

**Done when:** Agent can handle sites that throw alerts or confirm dialogs without hanging.

---

## Phase 3: Polish and Reliability (8 SP, ~1 week)

### 3.1 — Cookie/auth persistence (3 SP)
Safari's WebDriver runs in an **isolated automation profile** by default (orange URL bar). This means your normal Safari cookies aren't available.

**Two approaches:**
- **Accept the isolation** and use Browser Use's storage state save/load to persist cookies between runs (serialize via JS `document.cookie` + `localStorage` snapshots).
- **Use AppleScript** to control your normal Safari window instead of safaridriver. This gives you real cookies and iCloud Keychain but is less reliable for automation.

For practical use, the storage state approach works: log in once per site, save cookies, reload next run.

**Done when:** Agent can reuse a logged-in session across multiple runs.

### 3.2 — Error handling and recovery (3 SP)
Safari's WebDriver is less battle-tested than Chrome's CDP. You'll hit:
- Stale element references after page changes
- Timeout on slow pages
- Alert dialogs blocking execution

Build retry wrappers with exponential backoff for all driver calls. Add a `is_alive()` health check that the session calls before each action.

**Done when:** Agent doesn't hard-crash on common failure modes. Errors surface as `BrowserError` that the agent can reason about and retry.

### 3.3 — CLI flag and config (2 SP)
Add a `--browser safari` flag or config option so you can easily switch between Safari and Chromium.

```python
# In your wrapper script:
if args.browser == "safari":
    browser = SafariBrowserSession()
else:
    browser = BrowserSession()  # default CDP/Chromium

agent = Agent(task=task, browser=browser, llm=llm)
```

**Done when:** Single flag switches the entire backend.

---

## Phase 4: Stretch goals (5 SP, if you want more)

### 4.1 — AppleScript tab management (2 SP)
Supplement safaridriver's limited window handle API with AppleScript for richer tab control (get tab titles, reorder, detect tabs opened by JS).

### 4.2 — Downloads via AppleScript (3 SP)
Safaridriver doesn't support download management. Use AppleScript to detect Safari's download UI, poll `~/Downloads` for new files, and surface them to the agent.

---

## Execution order (critical path)

```
Week 1:  1.1 (driver.py) → 1.2 (DOM extractor)
Week 2:  2.1 (SafariBrowserSession) → 2.2 (DOM bridge)
Week 3:  2.3 (dialogs) → 3.1 (cookies) → 3.2 (error handling)
Week 4:  3.3 (CLI flag) → testing on real tasks → stretch goals
```

Start with `driver.py` because it's self-contained and immediately testable. The DOM extractor is the riskiest piece — if you can get that producing clean output on 4-5 common websites, everything else is wiring.

## Key files to study in the Browser Use codebase

```
browser_use/browser/session.py       # ~2500 lines, the main class you're replacing
browser_use/browser/events.py        # All events you need to handle (you've seen this)
browser_use/browser/views.py         # BrowserStateSummary, TabInfo, PageInfo structures
browser_use/browser/watchdogs/
  default_action_watchdog.py         # Handles click, type, scroll, navigate — study this most
  screenshot_watchdog.py             # Screenshot capture logic
  dom_watchdog.py                    # DOM extraction trigger and caching
  popups_watchdog.py                 # Dialog handling
browser_use/dom/
  service.py                         # DOM extraction and tree building
  views.py                           # EnhancedDOMTreeNode, SerializedDOMState
browser_use/tools/service.py         # Maps LLM tool calls to events
```
