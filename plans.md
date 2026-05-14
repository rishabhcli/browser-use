# Safari 1:1 Parity Feature Spec

Status: Draft
Scope: Decade-scale engineering plan
Primary target: Safari Technology Preview first, regular Safari second
Public goal: make Browser-Use work in Safari with the same user-facing functionality as the current Chromium/CDP implementation

## 1. Purpose

Browser-Use currently provides an LLM-driven browser automation loop over a Chromium-family browser. The public experience is simple: an `Agent` observes a page, decides actions through an LLM, executes those actions through browser tools, refreshes state, and repeats until the task is done.

The implementation underneath that public experience is not browser-neutral today. The current runtime is deeply tied to the Chrome DevTools Protocol (CDP): browser launch, tab discovery, DOM extraction, screenshots, input dispatch, downloads, permissions, storage state, network recording, video recording, and PDF generation all use CDP-specific primitives.

This document defines the feature specification and long-term engineering program required to provide 1:1 Safari support. "1:1" means the same public Browser-Use API, tool semantics, agent behavior, observable history, CLI workflow, and production operating model should work against Safari as they do against Chromium. When Safari does not expose an equivalent primitive, Browser-Use must either emulate the behavior or ship a helper service that provides the same user-facing outcome.

## 2. Definition of 1:1

1:1 parity is not merely launching Safari and clicking a button. It means all core Browser-Use promises continue to hold.

Required parity dimensions:

- Public API parity: existing `Agent`, `Browser`, `BrowserSession`, `Tools`, CLI, history, and output surfaces keep working.
- Behavioral parity: the same task prompt should produce materially equivalent results in Chromium and Safari.
- Tool parity: every default browser action has a Safari implementation with matching inputs, outputs, errors, and metadata.
- State parity: Safari browser state includes URL, title, tab list, viewport data, screenshot, DOM tree, selector map, page info, popup messages, and pending browser errors.
- Reliability parity: stale DOM handling, tab focus recovery, action sequence termination, retries, and failure messages remain strong.
- Observability parity: histories, screenshots, logs, downloads, traces, recordings, and structured action results remain available.
- Production parity: local browser, authenticated profile, cloud browser, remote control, scaling, proxy, captcha, and profile sync must have Safari answers.
- Security parity: allowed/prohibited domains, sensitive data redaction, permission policy, download safety, and storage isolation remain enforceable.
- Test parity: a conformance suite runs each feature against Chromium/CDP and Safari/WebDriver and compares behavior.

The only acceptable exception is a browser-engine hard limit. In that case, the product still needs a replacement path: WebDriver BiDi, JavaScript injection, OS automation, helper app, local proxy, Safari Web Extension, macOS virtual machine, or Browser-Use cloud infrastructure.

## 3. Local Baseline Observed

The local machine has Safari Technology Preview installed and usable through `safaridriver`.

Observed on 2026-05-14:

- Safari Technology Preview driver exists at `/Applications/Safari Technology Preview.app/Contents/MacOS/safaridriver`.
- WebDriver session creation works.
- Navigation works.
- Title, URL, window handles, and window rect work.
- Viewport screenshot works.
- JavaScript execution via WebDriver works.
- CSS element lookup works.
- Element click works.
- WebDriver `/print` endpoint returned `404 unknown command`.
- WebDriver BiDi can be requested at the session capability level, but the current returned capabilities did not include a usable websocket URL in the smoke probe.

This baseline makes Safari Technology Preview the correct first target. Regular Safari should be validated after the Safari Technology Preview backend is stable.

## 4. Current System Summary

The current repo shape separates the LLM agent from the browser implementation in concept, but not fully in code.

Important current surfaces:

- `browser_use.agent.service.Agent`: owns the loop, message manager, action execution, failure handling, history creation, and finalization.
- `browser_use.tools.service.Tools`: registers the default action set.
- `browser_use.browser.session.BrowserSession`: public browser facade, but currently assumes CDP.
- `browser_use.browser.session_manager.SessionManager`: CDP target/session manager.
- `browser_use.browser.watchdogs.*`: event handlers for launch, DOM, screenshots, actions, downloads, permissions, storage, popups, recording, HAR, security, captcha, and about:blank handling.
- `browser_use.dom.service.DomService`: builds the enhanced DOM tree using CDP DOMSnapshot, Accessibility, DOM, Runtime, and Page APIs.
- `browser_use.dom.views.EnhancedDOMTreeNode`: DOM node model used by selector maps, history, action execution, serialization, and extraction.
- `browser_use.skill_cli`: local browser CLI and daemon workflow.

Current browser dependency profile:

- Browser launch: Chrome-style process flags and `--remote-debugging-port`.
- Connection: CDP websocket from `/json/version`.
- Tabs: CDP Target domain.
- DOM: CDP DOMSnapshot, Accessibility, DOM, Runtime, Page frame tree.
- Input: CDP Input domain.
- Screenshot: CDP Page.captureScreenshot.
- PDF: CDP Page.printToPDF.
- Downloads: CDP download events and filesystem polling.
- Storage: CDP cookies, DOMStorage, localStorage/sessionStorage helpers.
- Permissions: CDP Browser.grantPermissions and Emulation.
- Network/HAR: CDP Network and Page lifecycle events.
- Video: CDP Page.startScreencast.
- Captcha cloud: Browser-Use cloud-specific browser events.

## 5. Target Architecture

Safari parity requires a real backend architecture, not scattered `if safari` branches.

### 5.1 Browser Backend Interface

Introduce a protocol-neutral backend layer beneath `BrowserSession`.

Proposed package layout:

```text
browser_use/browser/backends/
  __init__.py
  base.py
  capabilities.py
  errors.py
  cdp/
    backend.py
    session_manager.py
    watchdogs/
  safari/
    backend.py
    webdriver_client.py
    session_manager.py
    dom_engine.py
    action_engine.py
    storage_engine.py
    download_engine.py
    print_engine.py
    bidi_client.py
    diagnostics.py
```

Core interface:

```python
class BrowserBackend(Protocol):
    name: str
    capabilities: BrowserCapabilities

    async def start(self) -> BrowserStartResult: ...
    async def stop(self, force: bool = False) -> None: ...
    async def reconnect(self) -> None: ...

    async def get_tabs(self) -> list[TabInfo]: ...
    async def new_tab(self, url: str | None = None) -> str: ...
    async def switch_tab(self, tab_id: str) -> str: ...
    async def close_tab(self, tab_id: str) -> None: ...

    async def navigate(self, url: str, new_tab: bool = False) -> NavigationResult: ...
    async def go_back(self) -> None: ...
    async def go_forward(self) -> None: ...
    async def refresh(self) -> None: ...

    async def get_state(self, include_screenshot: bool, include_dom: bool) -> BrowserStateSummary: ...
    async def evaluate(self, code: str, await_promise: bool = True) -> Any: ...
    async def screenshot(self, full_page: bool = False, clip: dict | None = None) -> str: ...

    async def click_element(self, node: EnhancedDOMTreeNode) -> dict | None: ...
    async def click_coordinates(self, x: int, y: int, force: bool = False) -> dict | None: ...
    async def type_text(self, node: EnhancedDOMTreeNode, text: str, clear: bool) -> dict | None: ...
    async def send_keys(self, keys: str) -> None: ...
    async def scroll(self, amount: int, direction: str, node: EnhancedDOMTreeNode | None = None) -> None: ...
    async def upload_file(self, node: EnhancedDOMTreeNode, file_path: str) -> None: ...

    async def get_dropdown_options(self, node: EnhancedDOMTreeNode) -> dict[str, str]: ...
    async def select_dropdown_option(self, node: EnhancedDOMTreeNode, text: str) -> dict[str, str]: ...
    async def scroll_to_text(self, text: str, direction: str = "down") -> None: ...

    async def save_storage_state(self, path: str) -> StorageStateResult: ...
    async def load_storage_state(self, path: str) -> StorageStateResult: ...
    async def downloaded_files(self) -> list[str]: ...
    async def print_to_pdf(self, options: PdfOptions) -> bytes: ...
```

`BrowserSession` should become a stable facade. It should expose the same public methods, but delegate actual protocol work to the selected backend.

### 5.2 Backend Selection

New public configuration should be explicit but backward compatible.

Examples:

```python
browser = Browser(engine="chromium")
browser = Browser(engine="safari")
browser = Browser(engine="safari", channel="technology-preview")
browser = Browser(engine="safari", executable_path="/Applications/Safari Technology Preview.app")
```

Compatibility rules:

- Existing `Browser()` defaults to current Chromium behavior.
- Existing `cdp_url` means Chromium/CDP unless a future explicit backend supports the URL.
- `channel="safari-technology-preview"` selects Safari Technology Preview.
- `channel="safari"` selects regular Safari.
- Existing model names are never rewritten.
- Existing `BrowserProfile` fields remain accepted even when Safari cannot honor every one directly.

### 5.3 Capability Model

Every backend must report explicit capabilities.

```python
class BrowserCapabilities(BaseModel):
    engine: Literal["chromium", "safari"]
    protocol: Literal["cdp", "webdriver", "webdriver-bidi", "hybrid"]
    supports_headless: bool
    supports_full_page_screenshot: bool
    supports_pdf_print: bool
    supports_download_events: bool
    supports_network_events: bool
    supports_permission_grants: bool
    supports_arbitrary_user_data_dir: bool
    supports_extension_loading: bool
    supports_proxy_per_session: bool
    supports_video_recording: bool
    supports_cross_origin_frame_dom: bool
    supports_bidi: bool
```

The agent should not see this directly by default, but diagnostics and tests need it.

## 6. Feature Parity Matrix

### 6.1 Agent and LLM Features

| Feature | Current behavior | Safari strategy | Acceptance standard |
| --- | --- | --- | --- |
| `Agent.run()` | Starts browser, loops state to LLM to actions | Reuse unchanged through backend facade | Same task runs with `Browser(engine="safari")` |
| `ChatBrowserUse` default | Recommended model for automation | Unchanged | No Safari code changes model behavior |
| `max_steps`, `max_failures`, timeouts | Agent-level controls | Unchanged | Same failure semantics |
| `initial_actions` | Run deterministic actions before LLM loop | Backend executes same actions | Same action results |
| `max_actions_per_step` | Execute multiple actions until page changes | Backend reports URL/focus changes | Same termination behavior |
| `use_vision`, screenshots | Screenshot available to message manager | WebDriver screenshot and optional image overlays | Same observation shape |
| `page_extraction_llm` | Uses clean markdown from DOM tree | Safari DOM engine feeds same markdown path | Same extraction output quality |
| `sensitive_data` | Redacts sensitive values in action results/logs | Unchanged | No secret leakage |
| `save_conversation_path` | Saves model conversation | Unchanged | Same file format |
| `output_model_schema` | Pydantic structured final output | Unchanged | Same parsing behavior |
| action history | Stores model actions, URLs, screenshots | Unchanged data model | Same helper methods |

### 6.2 Browser Startup and Session Features

| Feature | Current behavior | Safari strategy | Acceptance standard |
| --- | --- | --- | --- |
| Local launch | Launch Chromium with CLI args and CDP port | Launch `safaridriver`, create Safari/STP WebDriver session | Safari window starts and session is usable |
| Existing browser/profile | Chrome `executable_path`, `user_data_dir`, `profile_directory` | Use Safari/STP automation profile policy; add profile manager if possible | Authenticated sessions work predictably |
| Regular Safari | Not supported | Use `/usr/bin/safaridriver` and regular Safari capability | Same tests pass after STP |
| Safari Technology Preview | Not supported | Use STP app and bundled STP `safaridriver` | Primary green target |
| Headless | Chrome headless args | Safari has no equivalent stable local headless mode | Provide cloud/macOS VM headless-like execution or mark unsupported in local capability |
| Window size | Chrome args and CDP metrics | WebDriver `setWindowRect` plus viewport measurement script | Same screenshot dimensions within tolerance |
| Window position | Chrome args | WebDriver `setWindowRect` where supported | Position applied or capability explains gap |
| Viewport | Chrome emulation/CDP metrics | Window rect plus JS visual viewport | Same `page_info` values |
| Device scale factor | CDP/device metrics | JS DPR observation; no forced DPR locally | Screenshot scaling normalized for LLM |
| User agent | Chrome launch arg | Safari cannot freely spoof through WebDriver; use proxy/header injection where possible | Capability and fallback documented |
| Keep alive | Leave browser process running | Leave WebDriver session/browser open where possible | No unwanted close |
| Reconnect | CDP reconnect/session manager rebuild | WebDriver status check, window-handle recovery, session recreation | Agent recovers from driver restart when possible |

### 6.3 State Observation

| Feature | Current behavior | Safari strategy | Acceptance standard |
| --- | --- | --- | --- |
| Current URL | CDP target info / Runtime | WebDriver `/url` | Exact |
| Title | CDP Runtime/Page | WebDriver `/title` | Exact |
| Tabs | CDP Target domain | WebDriver window handles | Same `TabInfo` shape |
| Focused tab | `agent_focus_target_id` | active window handle | Same switch behavior |
| DOM tree | CDP DOMSnapshot + AX tree + DOM document | Safari DOM Engine using injected JS, WebDriver elements, frame switching, optional BiDi | Same indexed elements for common pages |
| Selector map | CDP backend node ids | Synthetic stable Safari node ids plus WebDriver element ids | Index click/input works after observation |
| Screenshot | CDP `Page.captureScreenshot` | WebDriver screenshot | Same base64 field and saved files |
| Page info | CDP metrics | JS `visualViewport`, scroll metrics, document dimensions | Same fields |
| Pixels above/below | CDP/JS metrics | JS metrics | Same within tolerance |
| Pending requests | CDP Network | BiDi network or proxy; fallback unavailable | Same when network capture enabled |
| Popup messages | CDP/dialog watchdog | WebDriver alert APIs plus Safari prompt watcher | Same messages |
| PDF viewer detection | Chrome PDF viewer logic | Safari PDF viewer and URL/content heuristics | Same auto-download or extraction strategy |

### 6.4 Default Tool Actions

| Tool | Current behavior | Safari strategy | Acceptance standard |
| --- | --- | --- | --- |
| `search` | Build search URL, navigate | Same | Same result |
| `navigate` | `NavigateToUrlEvent` plus DOM empty retry | WebDriver URL navigation plus same empty DOM retry | Same error semantics |
| `go_back` | CDP page history | WebDriver back | Same |
| `wait` | asyncio sleep | Unchanged | Same |
| `click(index)` | Selector map to CDP node, mouse events, JS fallback | Selector map to WebDriver element, native click, W3C pointer fallback, JS fallback | Same action result and metadata |
| `click(x,y)` | CDP coordinate mouse events | W3C pointer actions | Same coordinate metadata |
| `input` | CDP typing, clear, verification | WebDriver clear/sendKeys plus JS verification | Same sensitive redaction and mismatch warning |
| `upload_file` | Find file input, CDP file chooser/set files | WebDriver file input sendKeys, JS visibility fallback, optional helper for hidden inputs | Upload succeeds without OS dialog |
| `switch` | CDP Target activate | WebDriver switch window | Same tab id convention |
| `close` | CDP Target close | WebDriver close window | Same |
| `extract` | Markdown from enhanced DOM tree | Safari DOM tree serialized through same markdown extractor | Same structured/free-text extraction |
| `search_page` | CDP Runtime JS | WebDriver execute script | Same output |
| `find_elements` | CDP Runtime JS | WebDriver execute script | Same output |
| `scroll` | CDP gestures and JS fallback | JS scroll, wheel actions where supported | Same page movement |
| `send_keys` | CDP key events | WebDriver actions/sendKeys with macOS key mapping | Same shortcut behavior |
| `find_text` | ScrollToText event | JS text walker plus scrollIntoView | Same |
| `screenshot` | CDP screenshot | WebDriver screenshot | Same file attachment behavior |
| `save_as_pdf` | CDP `Page.printToPDF` | Safari Print Service helper; WebDriver `/print` not currently available | Same PDF file output |
| `dropdown_options` | CDP/JS native and ARIA handling | WebDriver/JS native and ARIA handling | Same structured options |
| `select_dropdown` | CDP/JS selection | WebDriver select or JS dispatch events | Same success/error output |
| `write_file` | FileSystem service | Unchanged | Same |
| `replace_file` | FileSystem service | Unchanged | Same |
| `read_file` | FileSystem service | Unchanged | Same |
| `evaluate` | CDP Runtime evaluate | WebDriver execute script; async script for promises | Same return shape and errors |
| `done` | ActionResult final | Unchanged | Same |

### 6.5 Browser Profile Parameters

| Parameter | Safari requirement |
| --- | --- |
| `cdp_url` | Remains Chromium-only unless a compatibility shim is provided. Safari gets `webdriver_url` or backend-managed safaridriver. |
| `executable_path` | Accept STP/Safari app or driver path; normalize to safaridriver plus browser capability. |
| `channel` | Add `safari` and `safari-technology-preview` without breaking Chromium channels. |
| `args` | Safari does not accept Chrome args. Map only meaningful driver/app options. |
| `env` | Apply to safaridriver subprocess. |
| `headless` | Local Safari cannot satisfy true headless. Use cloud macOS VM execution for headless production parity. |
| `user_data_dir` | Safari does not support arbitrary Chrome-style profile dirs. Build profile strategy around STP separation, storage state, and optional macOS user/container isolation. |
| `profile_directory` | Chromium-only. Safari equivalent needs documented profile/container model. |
| `storage_state` | Support via WebDriver cookies plus per-origin JS storage. |
| `proxy` | Prefer system proxy, PAC, local proxy, or cloud proxy. Per-session local proxy may require helper. |
| `permissions` | Use Safari preferences, WebDriver prompts, TCC automation, or helper broker. |
| `headers` | Remote-only today. Safari needs proxy/header injection for parity. |
| `allowed_domains` | Enforce in Browser-Use layer before/after navigation. |
| `prohibited_domains` | Enforce in Browser-Use layer before/after navigation. |
| `enable_default_extensions` | Replace Chrome extensions with Safari Web Extensions or proxy/content filtering. |
| `cross_origin_iframes` | Use WebDriver frame switching, BiDi, and fallback screenshot/vision. |
| `downloads_path` | Safari lacks CDP download behavior. Use Safari preferences, filesystem watcher, and download reconciliation. |
| `auto_download_pdfs` | Implement through PDF detection plus HTTP fetch or print helper. |
| `record_video_dir` | Use OS screen capture, WebDriver screenshot stream, or cloud video pipeline. |
| `record_har_path` | Use BiDi network events or local proxy. |
| `captcha_solver` | Local Safari needs cloud/proxy solver or external provider. |
| `demo_mode` | Reimplement overlay injection via WebDriver JS and reinject on navigation. |

## 7. Safari Backend Deep Spec

### 7.1 WebDriver Client

Build an async WebDriver client rather than depending entirely on Selenium. This keeps Browser-Use async-native, avoids thread wrappers, and allows precise control over driver diagnostics.

Required endpoints:

- `GET /status`
- `POST /session`
- `DELETE /session/{id}`
- `POST /session/{id}/url`
- `GET /session/{id}/url`
- `GET /session/{id}/title`
- `GET /session/{id}/window/handles`
- `POST /session/{id}/window`
- `GET /session/{id}/window`
- `DELETE /session/{id}/window`
- `GET /session/{id}/window/rect`
- `POST /session/{id}/window/rect`
- `GET /session/{id}/screenshot`
- `POST /session/{id}/element`
- `POST /session/{id}/elements`
- `POST /session/{id}/element/{element_id}/click`
- `POST /session/{id}/element/{element_id}/clear`
- `POST /session/{id}/element/{element_id}/value`
- `POST /session/{id}/execute/sync`
- `POST /session/{id}/execute/async`
- `POST /session/{id}/actions`
- `DELETE /session/{id}/actions`
- `POST /session/{id}/back`
- `POST /session/{id}/forward`
- `POST /session/{id}/refresh`
- `GET /session/{id}/cookie`
- `POST /session/{id}/cookie`
- `DELETE /session/{id}/cookie`
- alert endpoints for accept/dismiss/text where supported

Client requirements:

- Pydantic v2 models for all internal request and response shapes.
- Strict WebDriver error mapping into Browser-Use errors.
- Driver process lifecycle management.
- Timeouts per command.
- Structured diagnostics for failed endpoints.
- Protocol feature probing during session start.
- Optional WebDriver BiDi client when Safari exposes a websocket URL.

### 7.2 Safari Session Manager

The Safari session manager replaces CDP Target sessions.

Responsibilities:

- Start `safaridriver` on a free local port.
- Create Safari or Safari Technology Preview sessions.
- Track WebDriver session id.
- Track window handles as tab ids.
- Maintain focused window handle as `agent_focus_target_id`.
- Convert window handles into `TabInfo`.
- Detect newly opened windows after click.
- Switch to new windows when current Chromium behavior would auto-switch.
- Recover focus if the active window closes.
- Cleanly close session and driver process.
- Keep browser alive when requested.
- Reconnect or fail gracefully when the WebDriver session becomes invalid.

Tab id rules:

- Preserve the existing short tab id UX by using the last four stable characters of the WebDriver window handle.
- Maintain a mapping from short id to full window handle.
- Avoid exposing Safari-specific handle strings in model-facing text unless needed.

### 7.3 Safari DOM Engine

This is the largest technical body of work.

Current Browser-Use quality depends on rich DOM state. Safari must recreate that without CDP DOMSnapshot.

The Safari DOM engine must:

- Execute an injected JavaScript DOM walker.
- Return a full protocol-neutral DOM tree.
- Include only useful visible/interactable nodes in the LLM representation.
- Preserve the existing `EnhancedDOMTreeNode` contract or introduce a compatible protocol-neutral replacement.
- Build `selector_map: dict[int, EnhancedDOMTreeNode]`.
- Include text nodes, attributes, roles, labels, aria fields, alt text, placeholders, values, hrefs, srcs, and form state.
- Include absolute and viewport-relative bounding boxes.
- Include computed style data needed for visibility and clickability.
- Traverse open shadow roots.
- Traverse same-origin iframes.
- Represent cross-origin iframes as boundary nodes with rects, URL, title if available, and fallback text where possible.
- Approximate paint order and occlusion.
- Detect clickable elements from tags, roles, tabindex, cursor, contenteditable, labels, event handler attributes, and heuristic listener detection.
- Avoid mutating page DOM unless an explicit temporary marker mode is enabled.
- Be fast enough for large pages.
- Return stable enough ids to support click/input after a state observation.

Synthetic node identity:

- Each state capture assigns a synthetic index for model-facing tools.
- Each node stores one or more locator strategies:
  - WebDriver element id if available.
  - CSS path.
  - XPath.
  - text signature.
  - role/name signature.
  - bounding box.
  - frame path.
  - shadow root path.
- Action execution tries locators in priority order.
- If a WebDriver element id goes stale, the backend re-resolves through CSS/XPath/signature.

Performance targets:

- Simple page DOM state under 500 ms.
- Typical content site under 1500 ms.
- Large app under 4000 ms with progressive pruning.
- Hard timeout with partial state rather than hanging.

### 7.4 State Serialization

Safari state must feed the same LLM-facing serializer as Chromium.

Requirements:

- Reuse `DOMTreeSerializer` where possible.
- Preserve index notation.
- Preserve important context like forms, buttons, links, inputs, dropdowns, table structure, and navigation.
- Include viewport and scroll state.
- Include current tabs list.
- Include popup/dialog messages.
- Include browser errors in state.
- Maintain token efficiency.

If `EnhancedDOMTreeNode` remains too CDP-shaped, introduce:

```python
class BrowserDOMNode(BaseModel):
    synthetic_id: int
    backend_node_id: int | None
    protocol_element_id: str | None
    node_name: str
    node_type: str
    attributes: dict[str, str]
    text: str
    children: list["BrowserDOMNode"]
    absolute_position: DOMRect | None
    viewport_position: DOMRect | None
    frame_path: list[str]
    locator: ElementLocator
```

Then provide compatibility adapters for existing code.

### 7.5 Action Engine

The Safari action engine implements the event handlers currently owned by `DefaultActionWatchdog`.

Click algorithm:

1. Resolve node to WebDriver element.
2. Scroll element into view.
3. Measure bounding box.
4. Check file input and select restrictions.
5. Check occlusion through `document.elementFromPoint`.
6. Try native WebDriver element click.
7. If native click fails, use W3C pointer actions at center point.
8. If pointer click fails and it is safe, use JavaScript `element.click()`.
9. Detect new tabs and downloads.
10. Return metadata including click coordinates and checkbox/radio state.

Type algorithm:

1. Resolve node.
2. For inputs/textareas/contenteditable, focus element.
3. Clear with WebDriver clear when requested.
4. Type with WebDriver sendKeys.
5. For contenteditable failures, use active element and keyboard actions.
6. Verify actual value/textContent.
7. Return mismatch warning metadata.

Key algorithm:

- Map Browser-Use key strings to W3C actions.
- Support macOS command shortcuts.
- Support Enter, Tab, Escape, Backspace, Delete, arrows, Home, End, PageUp, PageDown.
- Support chord syntax like `cmd+a`, `ctrl+l`, `Tab Tab Enter`.
- Preserve current behavior where the LLM can use keyboard navigation to recover from click issues.

Scroll algorithm:

- Page scroll via JavaScript `window.scrollBy`.
- Element scroll via resolved element and JS scroll on nearest scrollable container.
- Optional W3C wheel action where Safari supports it.
- Iframe scrolling through frame switching for same-origin frames.

Dropdown algorithm:

- Native `select` support through JS value assignment and `input/change` events.
- ARIA menu support through click/open/extract options.
- Fallback visible option text matching.

File upload algorithm:

- Detect closest file input.
- If visible, call WebDriver sendKeys with the local file path.
- If hidden but safe, temporarily adjust style or use JS to reveal, then sendKeys.
- Never open the macOS file picker as the main path.
- Validate file exists and has nonzero size before uploading.

### 7.6 Screenshot and Visual Layer

Safari screenshot parity requires:

- Viewport screenshot through WebDriver.
- Full-page screenshot via native support if available, otherwise scroll-and-stitch.
- Clip screenshot by cropping returned image when the driver lacks clip support.
- Hide Browser-Use highlights before screenshots.
- Highlight interactive elements using injected CSS overlays or post-processing.
- Normalize screenshot dimensions for LLM vision.
- Track original viewport size for coordinate conversion.

Full-page scroll-and-stitch requirements:

- Handle sticky headers.
- Handle lazy-loading.
- Handle dynamic layout shifts.
- Support maximum image size caps.
- Fall back to viewport screenshot with explicit metadata on failure.

### 7.7 PDF and Print Parity

Current Chromium parity relies on CDP `Page.printToPDF`. Safari Technology Preview WebDriver did not expose `/print` in the local smoke test. Therefore, Safari needs a helper path.

Required user-facing behavior:

- `save_as_pdf` returns a PDF file path exactly like Chromium.
- Clicking a print-related element can generate a PDF instead of opening a blocking print dialog.
- PDF file naming and duplicate handling match current behavior.
- The file is added to attachments and download tracking.

Implementation options:

1. Safari Print Service helper app:
   - Native macOS helper using WebKit or print APIs.
   - Receives current URL or serialized HTML.
   - Renders/prints to PDF.
   - Returns bytes to Browser-Use.

2. WebDriver plus macOS print automation:
   - Trigger print.
   - Control the print dialog.
   - Save to PDF.
   - Fragile and should be fallback only.

3. HTTP refetch plus HTML-to-PDF:
   - Refetch current URL with Safari cookies.
   - Render through a WebKit helper.
   - Strong for static pages, weaker for authenticated SPA state.

4. Cloud Safari PDF service:
   - Use macOS VM Safari/WebKit rendering remotely.
   - Best for production parity.

Acceptance:

- PDF generated for static pages, authenticated pages, SPAs, and print-button flows.
- Output size and visual layout are comparable to Safari print preview.
- No OS dialog is left open.

### 7.8 Downloads

Safari lacks CDP download events. A Safari download engine must emulate current behavior.

Requirements:

- Honor `downloads_path` where possible.
- Detect download start after click.
- Detect file completion.
- Track file name, path, size, extension, MIME type where possible.
- Report timeout vs in-progress vs complete.
- Support auto-download PDFs.
- Avoid confusing pre-existing files with new downloads.
- Work with duplicate filenames.

Implementation:

- Snapshot downloads directory before action.
- Use filesystem events or polling for new files.
- Recognize Safari partial download extensions.
- Use response URL and content-disposition when available through proxy/BiDi.
- For downloads triggered by same-origin URLs, optionally fetch directly with browser cookies.
- For remote/cloud Safari, expose remote files through Browser-Use storage API.

### 7.9 Storage, Auth, and Profiles

Current Browser-Use can use Chrome profiles and CDP storage state. Safari needs a different auth model.

Requirements:

- Login state can persist across runs.
- Cookies can be saved and restored.
- LocalStorage/sessionStorage can be saved and restored by origin.
- Storage import/export uses a stable JSON shape.
- Sensitive auth data is not logged.
- The user can choose STP isolated profile vs regular Safari.

Implementation:

- Use WebDriver cookie endpoints for current-domain cookies.
- Maintain origin inventory from visited pages and storage state files.
- For each origin, navigate to an origin bootstrap page and execute JS to read/write localStorage/sessionStorage.
- Provide optional Browser-Use profile sync helper for Safari/STP.
- For true isolated profiles, investigate:
  - STP separate app container.
  - macOS separate user account.
  - managed WebKit helper app.
  - cloud macOS VM profiles.

Acceptance:

- Authenticated task can log in once and reuse session later.
- Storage state file works for multiple origins.
- Storage restore happens before initial task navigation.

### 7.10 Network, HAR, Proxy, and Headers

CDP network events do not exist in Safari WebDriver today. Parity requires a hybrid path.

Requirements:

- HAR recording.
- Request/response metadata where available.
- Proxy support.
- Header injection for remote-like workflows.
- Download MIME detection.
- Pending network request observation.
- Captcha/proxy integration for production.

Implementation options:

- WebDriver BiDi network events where Safari exposes them.
- Local HTTP/S proxy with generated root certificate.
- Browser-Use cloud proxy for remote Safari.
- macOS Network Extension helper for advanced interception.

Acceptance:

- `record_har_path` writes a valid HAR.
- HTTPS request metadata is captured when network capture is enabled.
- Per-session proxy behavior can be achieved locally or through cloud.
- Lack of local per-session proxy is surfaced as a capability, not silent failure.

### 7.11 Permissions and Browser Prompts

Current CDP permission grants need a Safari strategy.

Required permissions:

- Clipboard read/write.
- Notifications.
- Camera.
- Microphone.
- Geolocation.
- Autoplay/media where relevant.

Implementation:

- Safari/STP preference preparation.
- WebDriver alert/prompt handling.
- macOS TCC automation for Browser-Use helper where possible.
- Browser-Use Permission Broker helper for preflight checks and user-visible diagnostics.
- Fallback instructions only when automation cannot legally/technically perform the grant.

Acceptance:

- Permission-dependent pages can be tested in Safari.
- The agent does not hang on permission prompts.
- Prompt decisions appear in browser state/history.

### 7.12 Extensions and Content Filtering

Current Chromium can load default extensions like ad blocking, cookie handling, and URL cleaning. Safari cannot load Chrome CRX extensions.

Safari parity requires:

- Safari Web Extension equivalents.
- Content blocker rules for ad/tracker blocking.
- Cookie banner handling through JS/action heuristics.
- URL cleaning through navigation wrapper/proxy.

Implementation:

- Build Browser-Use Safari Web Extension package.
- Provide installation and signing workflow.
- Enable extension in STP automation profile where possible.
- Fallback to proxy/content filtering when extension automation is unavailable.

Acceptance:

- `enable_default_extensions=True` has a Safari equivalent path.
- Pages that rely on cookie banners and ad-heavy layouts remain automatable.

### 7.13 Cross-Origin Iframes and Shadow DOM

Cross-origin iframe parity is one of the hardest areas.

Requirements:

- Same-origin iframes are fully traversed.
- Cross-origin iframes are represented, visible, and clickable at the boundary.
- The agent can switch into a cross-origin frame when WebDriver allows it.
- Open shadow roots are traversed.
- Closed shadow roots are handled by visual/coordinate fallback.

Implementation:

- DOM walker records frame tree and frame rects.
- WebDriver frame switching handles accessible frames.
- For inaccessible frames, expose frame element as an indexed element with visible metadata.
- Coordinate click fallback operates across frame boundaries.
- Vision mode is encouraged when DOM access is blocked.

Acceptance:

- OAuth/login iframes, payment iframes, embedded widgets, and shadow-heavy apps have conformance tests.

### 7.14 Popups, Dialogs, Alerts, and Print Dialogs

Requirements:

- Detect JS alerts, confirms, prompts.
- Auto-close or report dialogs according to current behavior.
- Handle beforeunload.
- Avoid deadlocks when a print dialog opens.
- Surface closed popup messages in browser state.

Implementation:

- WebDriver alert endpoints.
- macOS UI automation fallback only when WebDriver is insufficient.
- Print-button special handling routes to PDF helper before opening dialog.

Acceptance:

- Agent never hangs permanently on an alert/confirm/print dialog.

### 7.15 Recording and Diagnostics

Recording parity cannot rely on CDP screencast.

Options:

- Repeated WebDriver screenshots assembled into video.
- macOS ScreenCaptureKit helper.
- Cloud VM recording.

Requirements:

- `record_video_dir` creates a playable video file.
- Recording follows active tab/window.
- Recording starts and stops with session.
- Failure to record does not fail browser startup unless explicitly strict.

Diagnostics:

- Driver logs.
- WebDriver request/response traces.
- DOM state timing.
- Action timing.
- Screenshot metadata.
- Safari capability probe output.

## 8. CLI and Daemon Parity

Existing CLI commands should work against Safari sessions.

Required command parity:

- `browser-use open URL`
- `browser-use state`
- `browser-use click INDEX`
- `browser-use click X Y`
- `browser-use type TEXT`
- `browser-use input INDEX TEXT`
- `browser-use scroll`
- `browser-use screenshot`
- `browser-use tab list`
- `browser-use tab switch`
- `browser-use tab close`
- `browser-use upload`
- `browser-use select`
- `browser-use dropdown-options`
- `browser-use dblclick`
- `browser-use rightclick`
- `browser-use wait-for-selector`
- `browser-use cookies`
- `browser-use recording`
- `browser-use close`

CLI additions:

```bash
browser-use --engine safari open https://example.com
browser-use --engine safari --channel technology-preview state
browser-use safari doctor
browser-use safari enable-automation
browser-use safari capabilities
browser-use safari reset-profile
```

Daemon requirements:

- Session state includes backend engine and driver port.
- Stale safaridriver processes are detected.
- `close --all` cleans Safari sessions started by Browser-Use without killing user-owned Safari windows unless owned.
- Diagnostics identify whether Safari automation is enabled.

## 9. Cloud and Production Parity

Local Safari parity is not enough for true 1:1 production.

Production requirements:

- Provision Safari/STP browsers remotely.
- Stream the browser session.
- Persist profiles.
- Sync local authentication to remote Safari profile.
- Support proxy country selection.
- Support captcha handling.
- Support downloads, screenshots, videos, and logs.
- Scale to many concurrent sessions.

Likely architecture:

- macOS VM fleet.
- STP installed and pinned per image.
- safaridriver supervisor.
- Browser-Use agent sidecar close to browser.
- WebRTC/VNC streaming.
- Profile snapshots stored encrypted.
- Local-to-remote cookie/profile sync tool.
- Proxy layer at VM or network edge.
- Captcha solver integration.
- Artifact service for downloads, screenshots, HAR, recordings, and PDFs.

Acceptance:

- `Browser(engine="safari", use_cloud=True)` provisions a remote Safari browser.
- Authenticated cloud Safari task works with a synced profile.
- Cloud Safari has comparable latency to cloud Chromium for common tasks or reports capability/performance differences clearly.

## 10. Security Requirements

Safari support must not weaken Browser-Use security.

Requirements:

- Keep allowed/prohibited domain enforcement above the backend.
- Prevent wildcard TLD mistakes.
- Redact sensitive data in logs and histories.
- Avoid leaking storage state in diagnostics.
- Isolate profiles between users and tasks.
- Validate file upload paths.
- Validate download paths.
- Avoid arbitrary native automation unless explicitly inside the Browser-Use helper boundary.
- Sign and notarize helper apps used for Safari PDF, permissions, capture, or networking.
- Provide audit logs for cloud Safari sessions.

Threat model additions:

- safaridriver exposes a local HTTP server.
- macOS automation permissions can be abused if helper boundaries are loose.
- Profile sync may contain cookies and session tokens.
- Local proxy/root certificates are sensitive.
- Screen recording may capture private content.

## 11. Test and Conformance Program

1:1 Safari support must be proven by tests, not claimed.

### 11.1 Parity Harness

Build a harness that runs the same scenarios against:

- Chromium/CDP current backend.
- Safari Technology Preview WebDriver backend.
- Regular Safari WebDriver backend.
- Cloud Safari backend when available.

Each test records:

- Actions executed.
- Browser state text.
- Screenshot.
- Final result.
- Errors.
- Downloads/artifacts.
- Timing.

Comparison modes:

- Exact match for API shapes.
- Semantic match for browser outcomes.
- Screenshot similarity where useful.
- DOM index quality score.
- Task success rate.

### 11.2 Required Test Classes

Core tests:

- Start/stop.
- Navigate.
- Back/forward/refresh.
- Tabs.
- Screenshot.
- DOM state.
- Click button.
- Click link opening new tab.
- Type in input.
- Clear input.
- Checkbox/radio.
- Native select.
- ARIA dropdown.
- File upload.
- Scroll page.
- Scroll element.
- Search page.
- Find elements.
- Evaluate JS.
- Extract markdown.
- Save PDF.
- Download file.
- Alert/confirm/prompt.
- Permission prompt.
- Storage save/load.
- Authenticated profile.
- Cross-origin iframe.
- Shadow DOM.
- Large SPA.
- Long-running agent loop.
- Reconnect after driver crash.

Real-site task tests:

- Search engine query.
- E-commerce product extraction.
- Login with preserved profile.
- Calendar/form task.
- File upload form.
- PDF-heavy site.
- Cloudflare/captcha scenario through cloud.
- Multi-tab research task.

Performance tests:

- DOM capture latency.
- Screenshot latency.
- Click latency.
- Type latency.
- Agent step latency.
- Memory use.
- Driver stability over long sessions.

### 11.3 Quality Gates

Early alpha:

- 80 percent of core local deterministic tests pass in STP.
- No agent loop crashes on simple tasks.

Beta:

- 95 percent of core deterministic tests pass in STP.
- Regular Safari passes 90 percent.
- Known gaps have capability flags and clear errors.

Stable:

- 99 percent of core deterministic tests pass.
- Real-site task success rate within 10 percent of Chromium for supported tasks.
- No silent feature failure.
- Production Safari cloud available or explicitly separated from local support.

## 12. Roadmap

### Phase 0: Research and Driver Lab

Goals:

- Build a Safari driver probe suite.
- Record exact endpoint support for STP and Safari.
- Verify WebDriver BiDi availability across releases.
- Verify print, downloads, file upload, tabs, alerts, actions, screenshots, cookies, and frame switching.
- Document macOS automation prerequisites.

Deliverables:

- `browser-use safari doctor`.
- Capability probe JSON.
- Driver support matrix.
- Initial STP smoke tests in CI on macOS.

### Phase 1: Backend Abstraction

Goals:

- Extract protocol-neutral browser backend interface.
- Move CDP-specific logic behind CDP backend.
- Keep public API unchanged.
- Add capability model.

Deliverables:

- `BrowserBackend` protocol.
- CDP backend adapter.
- No regression in Chromium tests.
- Updated type models with Pydantic v2.

### Phase 2: Safari Minimal Session

Goals:

- Start safaridriver.
- Create STP session.
- Navigate.
- Get URL/title/tabs.
- Screenshot.
- Execute JS.
- Close.

Deliverables:

- `Browser(engine="safari", channel="technology-preview")`.
- Core WebDriver client.
- Minimal state without full DOM.
- CLI `open`, `screenshot`, `close`.

### Phase 3: Safari DOM State

Goals:

- Build Safari DOM walker.
- Create selector map.
- Serialize state for LLM.
- Support same-origin frames and open shadow roots.

Deliverables:

- Safari `BrowserStateSummary`.
- Indexed clickable elements.
- `state` CLI parity.
- DOM quality benchmark.

### Phase 4: Core Action Parity

Goals:

- Implement click, type, scroll, send keys, dropdowns, tabs.
- Implement page-change guards.
- Implement stale element recovery.

Deliverables:

- Default local deterministic action tests passing in STP.
- Multi-action agent tasks working.

### Phase 5: Extraction and Files

Goals:

- Make `extract`, `search_page`, `find_elements`, `evaluate`, upload, read/write files work identically.
- Ensure markdown extraction quality matches Chromium.

Deliverables:

- Extraction parity tests.
- File upload tests.
- Structured extraction tests.

### Phase 6: Storage and Auth

Goals:

- Save/load cookies and per-origin storage.
- Support persistent STP sessions.
- Add profile diagnostics.

Deliverables:

- Authenticated profile smoke test.
- Storage state import/export.
- Profile sync design.

### Phase 7: Downloads and PDF

Goals:

- Implement filesystem-based download tracking.
- Build PDF helper path.
- Support print-button interception.

Deliverables:

- `save_as_pdf` parity.
- Download event parity.
- PDF conformance tests.

### Phase 8: Network, Proxy, HAR, Permissions

Goals:

- Evaluate BiDi network events.
- Add proxy-based HAR fallback.
- Add permission broker.
- Support geolocation/media/clipboard workflows.

Deliverables:

- HAR output.
- Proxy support.
- Permission tests.

### Phase 9: Advanced Web Platform Coverage

Goals:

- Harden cross-origin iframes.
- Harden shadow DOM.
- Improve accessibility tree approximation.
- Add visual fallback for DOM-hostile pages.

Deliverables:

- Payment iframe tests.
- OAuth iframe tests.
- Shadow app tests.
- Vision-assisted fallback.

### Phase 10: Recording, Diagnostics, and Long-Run Reliability

Goals:

- Video recording.
- Robust reconnection.
- Driver crash recovery.
- Long-running sessions.

Deliverables:

- Recording artifacts.
- 24-hour stability test.
- Diagnostic bundle export.

### Phase 11: Cloud Safari

Goals:

- Provision remote Safari browsers.
- Support streaming, profiles, proxy, captcha, downloads, artifacts.
- Match local API.

Deliverables:

- `Browser(engine="safari", use_cloud=True)`.
- Cloud profile sync.
- Production monitoring.

### Phase 12: Stable 1:1 Release

Goals:

- Close all parity gaps or mark explicit hard browser limitations.
- Ship docs and migration guide.
- Maintain release-to-release Safari compatibility.

Deliverables:

- Stable Safari backend.
- Regular Safari support.
- STP rolling compatibility track.
- Conformance dashboard.

## 13. Decade-Scale Workstreams

This is the long-running program required if "same exact functionality" is interpreted literally.

### Workstream A: Protocol Abstraction

Owns the backend boundary, action interfaces, capability model, and migration away from CDP assumptions.

Long-term outcomes:

- Multiple browser engines can coexist.
- CDP remains first-class.
- Safari does not become a pile of special cases.
- Future WebDriver BiDi support can improve both Safari and other browsers.

### Workstream B: Safari Runtime

Owns safaridriver lifecycle, WebDriver client, BiDi client, session manager, window/tab handling, and reconnection.

Long-term outcomes:

- STP and Safari are continuously tested.
- Driver changes are detected early.
- Browser startup is reliable.

### Workstream C: DOM Intelligence

Owns state extraction, selector maps, accessibility, frame traversal, shadow DOM, paint order, visibility, and LLM representation.

Long-term outcomes:

- Safari state quality approaches or exceeds CDP state quality.
- DOM hostile pages fall back to vision/coordinate strategies.
- State extraction is fast and stable.

### Workstream D: Action Fidelity

Owns click, type, keyboard, scroll, drag/drop, hover, forms, dropdowns, uploads, dialogs, tabs, downloads, and stale-element recovery.

Long-term outcomes:

- Same task plans work across engines.
- The LLM does not need to know which browser is underneath.

### Workstream E: Artifacts and Observability

Owns screenshots, PDFs, videos, HAR, traces, logs, diagnostics, and history.

Long-term outcomes:

- Safari sessions are debuggable.
- Production failures are inspectable.
- Artifacts remain consistent across engines.

### Workstream F: Profiles and Authentication

Owns storage state, cookies, auth profile sync, cloud profiles, profile isolation, and sensitive data.

Long-term outcomes:

- Safari can do authenticated tasks locally and in cloud.
- Profiles can be synced safely.

### Workstream G: Cloud Safari

Owns macOS VM fleet, streaming, proxy, captcha, artifact storage, scaling, and production reliability.

Long-term outcomes:

- Safari support is not limited to a developer laptop.
- Production Browser-Use can choose Chromium or Safari per task.

### Workstream H: Conformance and Release Engineering

Owns parity tests, CI, real-site scenarios, benchmarks, release gates, and compatibility dashboards.

Long-term outcomes:

- Safari parity does not regress.
- Browser releases are tracked continuously.

## 14. Public API Proposal

Minimal user-facing API:

```python
from browser_use import Agent, Browser, ChatBrowserUse

browser = Browser(
    engine="safari",
    channel="technology-preview",
)

agent = Agent(
    task="Find the number 1 post on Show HN",
    browser=browser,
    llm=ChatBrowserUse(),
)

await agent.run()
```

Cloud API:

```python
browser = Browser(
    engine="safari",
    use_cloud=True,
    cloud_profile_id="profile-id",
    cloud_proxy_country_code="us",
)
```

Backward compatibility:

```python
browser = Browser()
```

continues to use Chromium/CDP exactly as before.

## 15. Error Semantics

Safari errors must be normalized.

Required error classes:

- `SafariDriverNotFoundError`
- `SafariAutomationNotEnabledError`
- `SafariSessionCreateError`
- `SafariUnsupportedFeatureError`
- `SafariElementStaleError`
- `SafariElementNotInteractableError`
- `SafariDownloadTimeoutError`
- `SafariPermissionBlockedError`
- `SafariProfileUnavailableError`
- `SafariBiDiUnavailableError`

Every unsupported feature must fail in one of two ways:

- Emulated path attempted and failed with clear context.
- Capability says unsupported and the action returns a useful `ActionResult(error=...)`.

Silent no-ops are unacceptable.

## 16. Documentation Requirements

Docs must cover:

- What Safari support means.
- Why STP is recommended first.
- How to enable Safari automation.
- Local setup.
- Cloud setup.
- Profiles and auth.
- Known hard limitations.
- Feature parity table.
- Troubleshooting.
- Security implications of safaridriver and helper apps.
- How to run conformance tests.

Docs should recommend `ChatBrowserUse` for browser automation tasks by default.

Docs should mention that `Browser(use_cloud=True)` remains the highest-performance production path where Browser-Use cloud supports the required browser.

## 17. Major Risks

Technical risks:

- Safari WebDriver lacks CDP-equivalent DOM, network, print, download, and permission APIs.
- WebDriver BiDi support may lag Chromium.
- Safari profile isolation is weaker than Chrome `user_data_dir`.
- OS automation can be brittle.
- Print/PDF parity may require native helper software.
- Cloud Safari requires macOS infrastructure, which is expensive and operationally complex.

Product risks:

- Users may expect local Safari to do cloud-only things.
- Some gaps may be impossible without Apple exposing more automation APIs.
- Maintaining 1:1 parity may slow Chromium backend changes unless conformance is strong.

Mitigations:

- Capability model.
- Conformance suite.
- Helper services for hard gaps.
- STP-first development.
- Clear docs.
- Backend boundary that prevents Safari complexity from polluting agent logic.

## 18. Acceptance Criteria for Full 1:1 Safari Release

Full release requires:

- `Browser(engine="safari", channel="technology-preview")` works for normal agent tasks.
- Regular Safari passes the supported local suite.
- All default Tools actions have Safari implementations.
- Browser state includes DOM, screenshots, tabs, page info, and selector map.
- `extract` quality is comparable to Chromium.
- File upload works.
- Downloads are tracked.
- PDF output works through a helper path.
- Storage/auth persistence works.
- Allowed/prohibited domain policies work.
- Permission prompts do not hang the agent.
- Popups/dialogs are handled.
- CLI works.
- History helpers work.
- Recording/HAR either work locally or have explicit helper/cloud support.
- Cloud Safari path exists for production-grade stealth, profiles, proxy, captcha, and scaling.
- Conformance dashboard is green at the agreed threshold.
- Known browser-engine limitations are documented and surfaced as capabilities.

## 19. First Implementation Slice

The smallest real slice that proves the architecture:

1. Add backend interface.
2. Wrap current CDP backend behind it with no behavior change.
3. Add Safari WebDriver client.
4. Add STP session start/stop.
5. Add navigate, URL, title, tabs, screenshot, execute JS.
6. Add JS DOM walker with selector map.
7. Add click and type.
8. Run one real `Agent` task in STP.
9. Add tests and `browser-use safari doctor`.

This first slice does not claim full parity. It proves the direction without corrupting the existing Chromium backend.

## 20. Final Principle

The browser backend can be different. The agent experience cannot be different.

The LLM should not have to think, "I am in Safari, so maybe clicking works differently." The user should not have to rewrite tasks. The public promise of Browser-Use should remain:

```text
Give the agent a browser, a task, and an LLM.
It observes, decides, acts, and finishes.
```

Safari support is successful only when Safari becomes another first-class browser engine under that promise, not a reduced demo path.
