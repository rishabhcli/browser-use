"""Browser backend capability models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class BrowserCapabilities(BaseModel):
	"""Feature support advertised by a browser backend.

	The agent does not consume this directly during normal operation; it is for
	diagnostics, tests, and explicit unsupported-feature errors.
	"""

	model_config = ConfigDict(extra='forbid')

	engine: Literal['chromium', 'safari']
	protocol: Literal['cdp', 'webdriver', 'webdriver-bidi', 'hybrid']
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
