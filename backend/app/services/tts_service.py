import asyncio
import logging
import re
import subprocess
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

from app.core.config import get_settings
from app.core.exceptions import TTSError

logger = logging.getLogger(__name__)
settings = get_settings()

TTS_URL = "https://ai.zalo.solutions/products/text-to-audio-converter"
TEXTAREA_SELECTOR = "textarea[placeholder='Enter your content here']"
CONSENT_BUTTON_SELECTOR = "button:has-text('UNDERSTAND')"
VOICE_SELECT_SELECTOR = "select#exampleFormControlSelect1"
GENERATE_BUTTON_SELECTOR = (
    "button:has-text('CONVERT INTO SPEECH'), "
    "button:has-text('Chuyển đổi'), "
    "button:has-text('Generate'), "
    "button:has-text('Convert'), "
    "button:has-text('Tạo âm thanh'), "
    "button[type='submit']"
)
# Timeout (ms) waiting for the audio API response after clicking Generate
AUDIO_RESPONSE_TIMEOUT_MS = 90_000


def _is_audio_response(response) -> bool:
    """Predicate for Playwright expect_response: matches Zalo TTS audio or HLS playlist (200 only)."""
    if response.status != 200:
        return False
    content_type = response.headers.get("content-type", "")
    url = response.url
    return (
        "audio/" in content_type
        or "application/octet-stream" in content_type
        or "tts.zalo.ai" in url  # catches chunk-v3 (direct MP3) and audiostream-v3 (HLS)
    )


class TTSService:
    """
    Text-to-speech service that drives the Zalo AI TTS web page
    using Playwright (sync API in a worker thread), processes text in
    chunks, and stitches the resulting audio files into a single MP3.
    """

    def __init__(self):
        self.output_dir = Path(settings.TTS_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.headless = settings.TTS_HEADLESS

    # ------------------------------------------------------------------
    # Public async entry point
    # ------------------------------------------------------------------

    async def text_to_speech(
        self,
        text: str,
        chunk_size: int | None = None,
        voice_id: int = 2,
    ) -> tuple[str, int]:
        """
        Convert text to speech.
        Runs Playwright in a worker thread (sync API) to avoid Windows
        SelectorEventLoop limitations with async subprocesses.

        Args:
            text:       Full text to synthesise.
            chunk_size: Max characters per TTS request chunk.
                        Defaults to TTS_CHUNK_SIZE from settings.
            voice_id:   Voice option value (1-6) from the Zalo TTS select element.

        Returns:
            (output_mp3_path, number_of_chunks_processed)
        """
        effective_chunk_size = chunk_size if chunk_size is not None else settings.TTS_CHUNK_SIZE
        chunks = self._split_text(text, effective_chunk_size)
        logger.info(f"TTS: {len(chunks)} chunk(s), chunk_size={effective_chunk_size}, voice_id={voice_id}, headless={self.headless}")
        return await asyncio.to_thread(self._run_sync, chunks, voice_id)

    # ------------------------------------------------------------------
    # Sync Playwright session (runs in worker thread)
    # ------------------------------------------------------------------

    def _run_sync(self, chunks: list[str], voice_id: int) -> tuple[str, int]:
        session_id = uuid.uuid4().hex[:12]
        chunk_paths: list[str] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context = browser.new_context()
            page = context.new_page()

            try:
                logger.info(f"Opening TTS page: {TTS_URL}")
                page.goto(TTS_URL, wait_until="networkidle", timeout=30_000)
                self._dismiss_consent(page)

                for idx, chunk in enumerate(chunks, 1):
                    logger.info(f"Processing chunk {idx}/{len(chunks)} ({len(chunk)} chars)")
                    # Reload page before each chunk (except the first, already loaded above)
                    # so the page is always in a clean input state, not showing a previous result.
                    if idx > 1:
                        logger.debug(f"Reloading TTS page before chunk {idx}")
                        page.goto(TTS_URL, wait_until="networkidle", timeout=30_000)
                        self._dismiss_consent(page)
                    audio_path = self._process_chunk(page, chunk, idx, voice_id, session_id)
                    chunk_paths.append(audio_path)

            finally:
                try:
                    browser.close()
                except Exception as e:
                    logger.debug(f"browser.close() ignored: {e}")

        if not chunk_paths:
            raise TTSError("No audio was generated")

        output_path = self._combine_audio(chunk_paths, session_id)
        self._cleanup_chunks(chunk_paths)
        return output_path, len(chunks)

    # ------------------------------------------------------------------
    # Text splitting
    # ------------------------------------------------------------------

    @staticmethod
    def _split_text(text: str, chunk_size: int) -> list[str]:
        """
        Split text into one sentence per chunk.

        The Zalo TTS site only processes up to the first sentence-ending
        punctuation (. ! ?) per submit — so each chunk must be a single
        sentence. chunk_size is only used as a hard limit to split
        abnormally long sentences that have no punctuation.
        """
        text = re.sub(r"\r\n|\r", "\n", text).strip()

        # Split on sentence-ending punctuation OR newlines.
        # Keep the punctuation attached to the sentence (lookbehind).
        # Negative lookbehind for digits protects "3.5", "TP.HCM" etc.
        parts = re.split(r"(?<!\d)(?<=[.!?…])\s*|\n+", text)
        sentences = [s.strip() for s in parts if s.strip()]

        chunks: list[str] = []
        for sentence in sentences:
            if len(sentence) <= chunk_size:
                chunks.append(sentence)
            else:
                # Hard-split only for extremely long sentences with no punctuation
                for i in range(0, len(sentence), chunk_size):
                    chunks.append(sentence[i : i + chunk_size])

        return chunks

    # ------------------------------------------------------------------
    # Browser automation helpers (sync)
    # ------------------------------------------------------------------

    def _dismiss_consent(self, page: Page) -> None:
        """Click the UNDERSTAND consent button if it appears."""
        try:
            btn = page.locator(CONSENT_BUTTON_SELECTOR)
            btn.wait_for(state="visible", timeout=5_000)
            btn.click()
            page.wait_for_load_state("networkidle", timeout=10_000)
            logger.debug("Consent dialog dismissed")
        except PlaywrightTimeoutError:
            logger.debug("No consent dialog found, continuing")

    def _select_voice(self, page: Page, voice_id: int) -> None:
        """
        Select the voice dropdown.
        The select element only appears in the DOM after the textarea is interacted with.
        Uses React fiber to fire the synthetic onChange so React state actually updates.
        """
        try:
            page.wait_for_selector(VOICE_SELECT_SELECTOR, timeout=10_000)

            result = page.evaluate(
                """
                (args) => {
                    const sel = document.querySelector(args.selector);
                    if (!sel) return 'element not found';

                    // Set the raw DOM value
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        HTMLSelectElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(sel, String(args.value));

                    // Walk React fiber tree to call the synthetic onChange handler
                    const fiberKey = Object.keys(sel).find(k =>
                        k.startsWith('__reactFiber') ||
                        k.startsWith('__reactInternalInstance') ||
                        k.startsWith('__reactEventHandlers')
                    );
                    if (fiberKey) {
                        let fiber = sel[fiberKey];
                        while (fiber) {
                            const onChange = fiber.memoizedProps && fiber.memoizedProps.onChange;
                            if (onChange) {
                                onChange({
                                    target: sel,
                                    currentTarget: sel,
                                    bubbles: true,
                                    persist: () => {},          // React 16 synthetic event compat
                                    nativeEvent: new Event('change'),
                                });
                                return 'React onChange fired, value=' + sel.value;
                            }
                            fiber = fiber.return;
                        }
                    }

                    // Fallback: native DOM change event
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    return 'DOM change event dispatched, value=' + sel.value;
                }
                """,
                {"selector": VOICE_SELECT_SELECTOR, "value": voice_id},
            )
            logger.info(f"Voice select result: {result}")
        except PlaywrightTimeoutError:
            logger.warning("Voice select not found after textarea fill, using page default")

    def _process_chunk(self, page: Page, chunk: str, idx: int, voice_id: int, session_id: str) -> str:
        """
        Fill textarea (triggers React to render the voice select) ->
        select voice -> click Generate -> intercept audio HTTP response -> save.
        """
        # Flatten newlines — Zalo TTS only reads the first line per submit.
        # Also remove control characters (null bytes, etc.) that can break JS evaluation.
        sanitized = re.sub(r"[\r\n]+", " ", chunk).strip()
        sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", sanitized)

        # 1. Click textarea so React mounts the full form (voice select appears after this)
        page.wait_for_selector(TEXTAREA_SELECTOR, timeout=15_000)
        page.click(TEXTAREA_SELECTOR)

        # 2. Set textarea value via JS nativeInputValueSetter so React's controlled
        #    component state is properly updated (page.fill() only updates the DOM value
        #    and may not fire React's synthetic onChange in all versions).
        result = page.evaluate(
            """
            (args) => {
                const ta = document.querySelector(args.selector);
                if (!ta) return 'textarea not found';

                // Clear first via native setter
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    HTMLTextAreaElement.prototype, 'value'
                ).set;
                nativeSetter.call(ta, '');
                ta.dispatchEvent(new Event('input', { bubbles: true }));

                // Set new value
                nativeSetter.call(ta, args.value);
                ta.dispatchEvent(new Event('input',  { bubbles: true }));
                ta.dispatchEvent(new Event('change', { bubbles: true }));

                // Also walk React fiber to call onChange directly
                const fiberKey = Object.keys(ta).find(k =>
                    k.startsWith('__reactFiber') ||
                    k.startsWith('__reactInternalInstance') ||
                    k.startsWith('__reactEventHandlers')
                );
                if (fiberKey) {
                    let fiber = ta[fiberKey];
                    while (fiber) {
                        const onChange = fiber.memoizedProps && fiber.memoizedProps.onChange;
                        if (onChange) {
                            onChange({
                                target: ta,
                                currentTarget: ta,
                                bubbles: true,
                                persist: () => {},          // React 16 synthetic event compat
                                nativeEvent: new Event('change'),
                            });
                            return 'React onChange fired, len=' + ta.value.length;
                        }
                        fiber = fiber.return;
                    }
                }
                return 'DOM events dispatched, len=' + ta.value.length;
            }
            """,
            {"selector": TEXTAREA_SELECTOR, "value": sanitized},
        )
        logger.info(f"  Chunk {idx}: textarea set via JS: {result}")

        # Verify textarea actually received the content; fall back to page.fill() if
        # the JS/React fiber approach silently failed (e.g. special file encoding).
        actual_len = page.evaluate(
            "(sel) => document.querySelector(sel)?.value?.length || 0",
            TEXTAREA_SELECTOR,
        )
        if actual_len == 0:
            logger.warning(f"  Chunk {idx}: JS fill produced empty textarea, falling back to page.fill()")
            page.fill(TEXTAREA_SELECTOR, sanitized)
        logger.info(f"  Chunk {idx}: textarea verified, len={actual_len or len(sanitized)}")

        # 3. Select voice — select is now in the DOM
        self._select_voice(page, voice_id)

        logger.info(f"  Chunk {idx}: clicking generate ({len(sanitized)} chars)...")

        generate_btn = page.locator(GENERATE_BUTTON_SELECTOR).first
        generate_btn.wait_for(state="visible", timeout=10_000)
        # Wait until the button is also enabled (React may keep it disabled until text state updates)
        try:
            page.wait_for_function(
                "(sel) => { const b = document.querySelector(sel); return b && !b.disabled; }",
                GENERATE_BUTTON_SELECTOR.split(",")[0].strip(),
                timeout=10_000,
            )
        except Exception:
            logger.warning(f"  Chunk {idx}: could not confirm button enabled, proceeding anyway")

        # Passively capture all tts.zalo.ai responses via event listener.
        # Using page.on("response") avoids route.fetch() errors that occur with
        # page.route() interception when the CDN uses strict origin checks.
        hls_buffer: dict[str, bytes] = {}

        def _on_response(response) -> None:
            try:
                if "tts.zalo.ai" in response.url and response.status == 200:
                    hls_buffer[response.url] = response.body()
            except Exception as e:
                logger.debug(f"  Chunk {idx}: response buffer error: {e}")

        page.on("response", _on_response)

        # Check for quota/error banners immediately after clicking, before waiting 90s
        def _check_page_error() -> str | None:
            try:
                return page.evaluate(
                    """
                    () => {
                        const selectors = [
                            '.alert', '.error', '.toast', '[class*="error"]',
                            '[class*="alert"]', '[class*="quota"]', '[class*="limit"]',
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.trim()) {
                                return el.innerText.trim();
                            }
                        }
                        return null;
                    }
                    """
                )
            except Exception:
                return None

        try:
            # expect_response registers BEFORE the click so no response is missed.
            with page.expect_response(
                _is_audio_response,
                timeout=AUDIO_RESPONSE_TIMEOUT_MS,
            ) as response_info:
                generate_btn.click()
                # Give the page ~2s to show any quota/error banner before we wait 90s
                page.wait_for_timeout(2_000)
                page_error = _check_page_error()
                if page_error:
                    raise TTSError(f"TTS site error: {page_error}")

            response = response_info.value
            url = response.url
            logger.info(f"  Chunk {idx}: audio response from {url} (status {response.status})")

            # Handle HLS playlist (m3u8) vs direct audio
            if ".m3u8" in url:
                logger.info(f"  Chunk {idx}: HLS playlist detected, collecting via response buffer...")
                data = self._collect_hls_from_buffer(page, url, hls_buffer, idx)
            else:
                data = response.body()
                if not data:
                    raise TTSError(f"Empty audio body received for chunk {idx}")
        finally:
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass

        output_path = str(self.output_dir / f"{session_id}_chunk_{idx}.mp3")
        with open(output_path, "wb") as f:
            f.write(data)
        logger.info(f"  Chunk {idx}: saved {len(data)} bytes → {output_path}")
        return output_path

    # ------------------------------------------------------------------
    # HLS helper
    # ------------------------------------------------------------------

    def _collect_hls_from_buffer(
        self, page: Page, m3u8_url: str, hls_buffer: dict, chunk_idx: int
    ) -> bytes:
        """
        Wait for the m3u8 playlist and all its segments to appear in hls_buffer
        (populated by the page.route() interceptor), then convert to MP3.
        Handles both master playlists (EXT-X-STREAM-INF) and media playlists.
        """
        # 1. Wait for m3u8 body (should already be buffered, but give it 5 s)
        for _ in range(50):
            if m3u8_url in hls_buffer:
                break
            page.wait_for_timeout(100)
        else:
            raise TTSError(f"m3u8 not captured in route buffer for chunk {chunk_idx}")

        m3u8_text = hls_buffer[m3u8_url].decode("utf-8")
        logger.debug(f"  Chunk {chunk_idx}: m3u8 content:\n{m3u8_text[:500]}")
        base_url = m3u8_url.rsplit("/", 1)[0] + "/"

        def _resolve(line: str, base: str) -> str:
            s = line.strip()
            if s.startswith("http"):
                return s
            if s.startswith("//"):
                return "https:" + s  # protocol-relative URL
            return base + s

        def _parse_segments(text: str, base: str) -> list[str]:
            """Extract segment URLs: standard #EXTINF lines and #EXT-X-PRELOAD-HINT URIs."""
            import re as _re
            segs = []
            lines = text.splitlines()
            for i, line in enumerate(lines):
                s = line.strip()
                if s.startswith("#EXTINF"):
                    # Next non-empty, non-comment line is the segment URL
                    for j in range(i + 1, len(lines)):
                        nxt = lines[j].strip()
                        if nxt and not nxt.startswith("#"):
                            segs.append(_resolve(nxt, base))
                            break
                elif s.startswith("#EXT-X-PRELOAD-HINT"):
                    # LL-HLS: #EXT-X-PRELOAD-HINT:TYPE=PART,URI="//chunk-v3..."
                    m = _re.search(r'URI=["\']([^"\']+)["\']', s)
                    if m:
                        segs.append(_resolve(m.group(1), base))
            return segs

        def _get_m3u8_text() -> str:
            return hls_buffer.get(m3u8_url, b"").decode("utf-8")

        # 2. Detect master playlist (contains #EXT-X-STREAM-INF or exactly #EXT-X-MEDIA:)
        lines = m3u8_text.splitlines()
        is_master = any(
            l.strip().startswith("#EXT-X-STREAM-INF") or l.strip().startswith("#EXT-X-MEDIA:")
            for l in lines
        )

        if is_master:
            # Pick the first variant playlist URL
            media_playlist_url = next(
                (_resolve(l, base_url) for l in lines if l.strip() and not l.strip().startswith("#")),
                None,
            )
            if not media_playlist_url:
                raise TTSError(f"No variant URL in master playlist for chunk {chunk_idx}")
            logger.info(f"  Chunk {chunk_idx}: master playlist → media playlist {media_playlist_url}")

            # Wait for the browser to fetch and buffer the media playlist
            for _ in range(50):
                if media_playlist_url in hls_buffer:
                    break
                page.wait_for_timeout(100)
            else:
                raise TTSError(f"Media playlist not captured in route buffer for chunk {chunk_idx}")

            m3u8_text = hls_buffer[media_playlist_url].decode("utf-8")
            base_url = media_playlist_url.rsplit("/", 1)[0] + "/"

        # 3. Wait for m3u8 to contain a real segment (LL-HLS sends partial playlist first).
        #    The browser re-fetches the m3u8; _on_response overwrites hls_buffer[m3u8_url]
        #    with the latest version each time, so keep re-reading until #EXTINF appears or
        #    a PRELOAD-HINT is present, max 30 s.
        for _ in range(150):
            current_text = _get_m3u8_text()
            if "#EXTINF" in current_text or "#EXT-X-PRELOAD-HINT" in current_text:
                m3u8_text = current_text
                break
            page.wait_for_timeout(200)

        segments = _parse_segments(m3u8_text, base_url)
        if not segments:
            raise TTSError(
                f"Empty HLS media playlist for chunk {chunk_idx}. "
                f"Playlist content: {m3u8_text[:300]}"
            )
        logger.info(f"  Chunk {chunk_idx}: HLS has {len(segments)} segment(s), waiting...")

        # 4. Wait for the browser to fetch all segments (auto-play drives this), max 30 s
        for _ in range(150):
            if all(s in hls_buffer for s in segments):
                break
            page.wait_for_timeout(200)
        missing = [s for s in segments if s not in hls_buffer]
        if missing:
            raise TTSError(
                f"Timeout: {len(missing)}/{len(segments)} HLS segments not buffered for chunk {chunk_idx}"
            )

        # 5. Concatenate raw segment bytes in playlist order
        raw = bytearray()
        for seg_url in segments:
            raw.extend(hls_buffer[seg_url])

        # 4. Convert TS/AAC → MP3 via FFmpeg
        raw_file = str((self.output_dir / f"hls_raw_{uuid.uuid4().hex[:8]}.ts").resolve())
        out_file = str((self.output_dir / f"hls_tmp_{uuid.uuid4().hex[:8]}.mp3").resolve())
        Path(raw_file).write_bytes(bytes(raw))
        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", raw_file,
                "-vn",
                "-c:a", "libmp3lame",
                "-q:a", "2",
                out_file,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise TTSError(
                    f"FFmpeg HLS convert error for chunk {chunk_idx}: {result.stderr[-500:]}"
                )
            data = Path(out_file).read_bytes()
        finally:
            Path(raw_file).unlink(missing_ok=True)
            Path(out_file).unlink(missing_ok=True)

        if not data:
            raise TTSError(f"No audio data after HLS convert for chunk {chunk_idx}")
        return data

    # ------------------------------------------------------------------
    # Audio stitching
    # ------------------------------------------------------------------

    def _combine_audio(self, chunk_paths: list[str], session_id: str) -> str:
        """Concatenate multiple MP3 files into one using FFmpeg concat demuxer."""
        final = str((self.output_dir / f"{session_id}.mp3").resolve())
        if len(chunk_paths) == 1:
            # No need to concat — just rename/move
            Path(chunk_paths[0]).rename(final)
            return final

        # Build FFmpeg command using the concat filter + re-encode.
        # Using `-c copy` with MP3 embeds ID3 tags and VBR headers mid-stream,
        # producing unplayable output. Re-encoding with libmp3lame produces a
        # clean, properly-headered MP3.
        inputs: list[str] = []
        for path in chunk_paths:
            inputs += ["-i", str(Path(path).resolve())]

        n = len(chunk_paths)
        filter_complex = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[outa]"

        output_path = final
        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[outa]",
            "-c:a", "libmp3lame",
            "-q:a", "2",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise TTSError(f"FFmpeg concat error: {result.stderr[-300:]}")

        logger.info(f"Combined audio saved: {output_path}")
        return output_path

    def _cleanup_chunks(self, paths: list[str]) -> None:
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as e:
                logger.warning(f"Could not remove chunk {path}: {e}")
