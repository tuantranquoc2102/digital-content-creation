import asyncio
import logging
import re
import subprocess
import time
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

# Retry settings for each chunk
CHUNK_MAX_RETRIES = 3        # max attempts per chunk (1 original + 2 retries)
CHUNK_RETRY_DELAY_S = 5     # base delay seconds; multiplied by attempt number


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
        session_id: str | None = None,
    ) -> tuple[str, int, str]:
        """
        Convert text to speech.
        Runs Playwright in a worker thread (sync API) to avoid Windows
        SelectorEventLoop limitations with async subprocesses.

        Args:
            text:       Full text to synthesise.
            chunk_size: Max characters per TTS request chunk.
                        Defaults to TTS_CHUNK_SIZE from settings.
            voice_id:   Voice option value (1-6) from the Zalo TTS select element.
            session_id: Optional. If provided, any already-completed chunk files
                        from a previous run are reused (resume from failure).
                        The session_id is returned so it can be passed on a retry.

        Returns:
            (output_mp3_path, number_of_chunks_processed, session_id)
        """
        effective_chunk_size = chunk_size if chunk_size is not None else settings.TTS_CHUNK_SIZE
        chunks = self._split_text(text, effective_chunk_size)
        resolved_session_id = session_id or uuid.uuid4().hex[:12]
        logger.info(
            f"TTS: {len(chunks)} chunk(s), chunk_size={effective_chunk_size}, "
            f"voice_id={voice_id}, session_id={resolved_session_id}, headless={self.headless}"
        )
        output_path, chunks_done = await asyncio.to_thread(
            self._run_sync, chunks, voice_id, resolved_session_id
        )
        return output_path, chunks_done, resolved_session_id

    # ------------------------------------------------------------------
    # Sync Playwright session (runs in worker thread)
    # ------------------------------------------------------------------

    def _run_sync(self, chunks: list[str], voice_id: int, session_id: str) -> tuple[str, int]:
        chunk_paths: list[str] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)

            try:
                for idx, chunk in enumerate(chunks, 1):
                    expected_path = str(self.output_dir / f"{session_id}_chunk_{idx}.mp3")

                    # ── Resume: reuse an already-completed chunk file ──────────────
                    if Path(expected_path).exists() and Path(expected_path).stat().st_size > 0:
                        logger.info(f"Chunk {idx}/{len(chunks)}: reusing existing file (resume)")
                        chunk_paths.append(expected_path)
                        continue

                    logger.info(f"Processing chunk {idx}/{len(chunks)} ({len(chunk)} chars)")

                    # ── Per-chunk retry with a fresh browser context each attempt ──
                    # A fresh context (= fresh incognito session) avoids per-session
                    # quota limits that disable the Convert button after ~10 requests.
                    last_error: Exception | None = None
                    for attempt in range(1, settings.TTS_CHUNK_MAX_RETRIES + 1):
                        if attempt > 1:
                            delay = settings.TTS_CHUNK_RETRY_DELAY_S * (attempt - 1)
                            logger.warning(
                                f"  Chunk {idx}: retry {attempt}/{settings.TTS_CHUNK_MAX_RETRIES} "
                                f"in {delay}s… (last error: {last_error})"
                            )
                            time.sleep(delay)

                        context = browser.new_context()
                        page = context.new_page()
                        try:
                            page.goto(TTS_URL, wait_until="networkidle", timeout=30_000)
                            self._dismiss_consent(page)
                            audio_path = self._process_chunk(page, chunk, idx, voice_id, session_id)
                            chunk_paths.append(audio_path)
                            last_error = None
                            break  # success — move to next chunk
                        except Exception as e:
                            last_error = e
                            logger.warning(f"  Chunk {idx}: attempt {attempt} failed: {e}")
                        finally:
                            try:
                                context.close()
                            except Exception:
                                pass

                    if last_error is not None:
                        raise TTSError(
                            f"Chunk {idx}/{len(chunks)} failed after "
                            f"{settings.TTS_CHUNK_MAX_RETRIES} attempt(s). "
                            f"Pass session_id='{session_id}' to resume from this chunk. "
                            f"Last error: {last_error}"
                        ) from last_error

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
        Split text into chunks of at most chunk_size characters, combining
        consecutive sentences (separated by . ! ? … or newlines) until the
        chunk would exceed chunk_size. Abnormally long sentences without
        punctuation are hard-split at chunk_size boundaries.
        """
        text = re.sub(r"\r\n|\r", "\n", text).strip()

        # Split on sentence-ending punctuation OR newlines.
        # Keep the punctuation attached to the sentence (lookbehind).
        # Negative lookbehind for digits protects "3.5", "TP.HCM" etc.
        parts = re.split(r"(?<!\d)(?<=[.!?…])\s*|\n+", text)
        sentences = [s.strip() for s in parts if s.strip()]

        chunks: list[str] = []
        current_parts: list[str] = []
        current_len = 0

        for sentence in sentences:
            slen = len(sentence)
            if slen > chunk_size:
                # Flush accumulated sentences first
                if current_parts:
                    chunks.append(" ".join(current_parts))
                    current_parts, current_len = [], 0
                # Hard-split the oversized sentence
                for i in range(0, slen, chunk_size):
                    chunks.append(sentence[i : i + chunk_size])
            else:
                sep = 1 if current_parts else 0
                if current_len + sep + slen > chunk_size:
                    chunks.append(" ".join(current_parts))
                    current_parts, current_len = [sentence], slen
                else:
                    current_parts.append(sentence)
                    current_len += sep + slen

        if current_parts:
            chunks.append(" ".join(current_parts))

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

        # Blur the textarea so React re-evaluates state and enables the Generate button.
        # The Zalo TTS site requires a focus→type→blur cycle; without blur the button
        # stays disabled (user reports clicking outside the textarea fixes it).
        page.evaluate(
            """
            (sel) => {
                const ta = document.querySelector(sel);
                if (ta) {
                    ta.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
                    ta.blur();
                }
            }
            """,
            TEXTAREA_SELECTOR,
        )
        page.wait_for_timeout(500)  # allow React to re-render after blur
        logger.debug(f"  Chunk {idx}: textarea blurred to trigger React state update")

        # 3. Select voice — select is now in the DOM
        self._select_voice(page, voice_id)

        logger.info(f"  Chunk {idx}: clicking generate ({len(sanitized)} chars)...")

        generate_btn = page.locator(GENERATE_BUTTON_SELECTOR).first
        generate_btn.wait_for(state="visible", timeout=10_000)
        # Wait until the button is also enabled (React may keep it disabled until text state updates).
        # We check ALL candidate selectors so any one of them being enabled counts.
        try:
            page.wait_for_function(
                """
                (selectors) => {
                    for (const sel of selectors) {
                        try {
                            const b = document.querySelector(sel);
                            if (b && !b.disabled) return true;
                        } catch (_) {}
                    }
                    return false;
                }
                """,
                [s.strip() for s in GENERATE_BUTTON_SELECTOR.split(",")],
                timeout=15_000,
            )
        except Exception:
            logger.warning(f"  Chunk {idx}: could not confirm button enabled, proceeding anyway")

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
                logger.info(f"  Chunk {idx}: HLS playlist detected, fetching segments directly...")
                data = self._fetch_hls_audio(page, url, idx)
            else:
                data = response.body()
                if not data:
                    raise TTSError(f"Empty audio body received for chunk {idx}")
        except TTSError:
            raise
        except Exception as e:
            raise TTSError(f"Chunk {idx}: unexpected error: {e}") from e

        output_path = str(self.output_dir / f"{session_id}_chunk_{idx}.mp3")
        with open(output_path, "wb") as f:
            f.write(data)
        logger.info(f"  Chunk {idx}: saved {len(data)} bytes → {output_path}")
        return output_path

    # ------------------------------------------------------------------
    # HLS helper — direct HTTP download
    # ------------------------------------------------------------------

    def _fetch_hls_audio(self, page: Page, m3u8_url: str, chunk_idx: int) -> bytes:
        """
        Download an HLS stream by fetching the playlist and every segment
        directly via Playwright's request context (shares browser cookies/
        session tokens embedded in the CDN URL).

        This replaces the previous passive-buffer approach, which relied on
        the browser's media player auto-fetching all segments — that only
        prefetched the first ~12 segments before stopping, leaving the rest
        un-buffered and causing the '19/31 HLS segments not buffered' error.
        """

        def _get_bytes(url: str) -> bytes:
            resp = page.request.get(url, timeout=30_000)
            if not resp.ok:
                raise TTSError(f"HTTP {resp.status} fetching {url}")
            return resp.body()

        def _get_text(url: str) -> str:
            resp = page.request.get(url, timeout=30_000)
            if not resp.ok:
                raise TTSError(f"HTTP {resp.status} fetching {url}")
            return resp.text()

        def _resolve(seg: str, base: str) -> str:
            s = seg.strip()
            if s.startswith("http"):
                return s
            if s.startswith("//"):
                return "https:" + s
            return base + s

        def _parse_segments(text: str, base: str) -> list[str]:
            segs: list[str] = []
            lines = text.splitlines()
            for i, line in enumerate(lines):
                s = line.strip()
                if s.startswith("#EXTINF"):
                    for j in range(i + 1, len(lines)):
                        nxt = lines[j].strip()
                        if nxt and not nxt.startswith("#"):
                            segs.append(_resolve(nxt, base))
                            break
                elif s.startswith("#EXT-X-PRELOAD-HINT"):
                    m = re.search(r'URI=["\']([^"\']+)["\']', s)
                    if m:
                        segs.append(_resolve(m.group(1), base))
            return segs

        base_url = m3u8_url.rsplit("/", 1)[0] + "/"

        # 1. Fetch the top-level playlist
        m3u8_text = _get_text(m3u8_url)
        logger.debug(f"  Chunk {chunk_idx}: m3u8 content:\n{m3u8_text[:500]}")

        # 2. Resolve master playlist → media playlist
        lines = m3u8_text.splitlines()
        is_master = any(
            l.strip().startswith("#EXT-X-STREAM-INF") or l.strip().startswith("#EXT-X-MEDIA:")
            for l in lines
        )
        if is_master:
            media_url = next(
                (_resolve(l, base_url) for l in lines if l.strip() and not l.strip().startswith("#")),
                None,
            )
            if not media_url:
                raise TTSError(f"No variant URL in master playlist for chunk {chunk_idx}")
            logger.info(f"  Chunk {chunk_idx}: master → media playlist {media_url}")
            base_url = media_url.rsplit("/", 1)[0] + "/"
            m3u8_url = media_url
            m3u8_text = _get_text(m3u8_url)

        # 3. For live/LL-HLS streams: poll until EXT-X-ENDLIST appears (max 120 s)
        if "#EXT-X-ENDLIST" not in m3u8_text:
            deadline = time.time() + 120
            while time.time() < deadline:
                time.sleep(2)
                try:
                    m3u8_text = _get_text(m3u8_url)
                    logger.debug(f"  Chunk {chunk_idx}: waiting for EXT-X-ENDLIST…")
                    if "#EXT-X-ENDLIST" in m3u8_text:
                        break
                except Exception:
                    pass
            else:
                logger.warning(
                    f"  Chunk {chunk_idx}: EXT-X-ENDLIST not found after 120s, using partial playlist"
                )

        # 4. Parse the complete segment list
        segments = _parse_segments(m3u8_text, base_url)
        if not segments:
            raise TTSError(
                f"Empty HLS media playlist for chunk {chunk_idx}. "
                f"Playlist content: {m3u8_text[:300]}"
            )
        logger.info(f"  Chunk {chunk_idx}: downloading {len(segments)} HLS segment(s) directly…")

        # 5. Download every segment in playlist order
        raw = bytearray()
        for i, seg_url in enumerate(segments, 1):
            logger.debug(f"  Chunk {chunk_idx}: segment {i}/{len(segments)}: {seg_url}")
            raw.extend(_get_bytes(seg_url))

        # 6. Convert TS/AAC → MP3 via FFmpeg
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
