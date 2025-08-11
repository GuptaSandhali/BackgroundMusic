from flask import Flask, request, jsonify, send_file
import requests
import os
import tempfile
from pydub import AudioSegment
import io
from urllib.parse import urlparse, parse_qs
import uuid
import logging

# =========================
# Config
# =========================

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Fixed background music URL (Google Drive share link)
BACKGROUND_MUSIC_URL = "https://drive.google.com/file/d/1y5MbuIq01IldamB9HdxvmDSx4wfcn7qr/view?usp=sharing"

# 🔒 Fixed intro/outro you provided
BEGINNING_AUDIO_URL = "https://drive.google.com/file/d/1w604zYgdQCcSpx-yd0WR7gWwj9ZCNRfb/view?usp=drive_link"
ENDING_AUDIO_URL    = "https://drive.google.com/file/d/1-2SWAFnyAQsSTNqs2rqnDySL1mV40zvJ/view?usp=drive_link"

# 🔒 Fixed default mix parameters you requested
DEFAULT_BEGINNING_VOLUME   = 0        # dB
DEFAULT_ENDING_VOLUME      = 0        # dB
DEFAULT_GAP_BEFORE_MS      = 250      # ms, used only when no intro crossfade
DEFAULT_GAP_AFTER_MS       = 0        # ms, used only when no outro crossfade
DEFAULT_CROSSFADE_INTRO_MS = 500      # ms
DEFAULT_CROSSFADE_OUTRO_MS = 300      # ms

# Other defaults
DEFAULT_VOICE_VOLUME       = 0        # dB
DEFAULT_BACKGROUND_VOLUME  = -12      # dB
DEFAULT_OUTPUT_FORMAT      = "mp3"    # mp3/aac/wav etc.


# =========================
# Helpers
# =========================

def _normalize_optional_url(url):
    """Treat empty strings / None as not provided."""
    if url is None:
        return None
    s = str(url).strip()
    return s if s else None


def download_from_gdrive(share_url, output_path):
    """Download file from a Google Drive sharing URL into output_path."""
    try:
        file_id = None

        if "drive.google.com" in share_url:
            if "/file/d/" in share_url:
                file_id = share_url.split("/file/d/")[1].split("/")[0]
            else:
                parsed_url = urlparse(share_url)
                query_params = parse_qs(parsed_url.query)
                file_id = query_params.get('id', [None])[0]
                if not file_id and 'file/d/' in share_url:
                    file_id = share_url.split('file/d/')[1].split('/')[0]

        if not file_id:
            raise ValueError("Could not extract file ID from Google Drive URL")

        # Direct download URL
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        logger.info(f"Downloading from Google Drive: {file_id}")

        # Initial request
        response = requests.get(download_url, stream=True, timeout=60)

        # If Google shows an interstitial (virus scan / warning), follow confirm link
        if response.status_code == 200 and ('text/html' in response.headers.get('Content-Type', '').lower()):
            try:
                lower = response.text.lower()
            except Exception:
                lower = ""
            if 'download_warning' in lower or 'virus scan' in lower or 'download anyway' in lower:
                import re
                for line in response.text.split('\n'):
                    if 'download_warning' in line and 'href' in line:
                        m = re.search(r'href="([^"]*)"', line)
                        if m:
                            confirm_url = m.group(1).replace('&amp;', '&')
                            response = requests.get(f"https://drive.google.com{confirm_url}", stream=True, timeout=60)
                            break

        if response.status_code == 200:
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            logger.info(f"File downloaded successfully: {output_path}")
            return True

        logger.error(f"Failed to download file. Status code: {response.status_code}")
        return False

    except Exception as e:
        logger.error(f"Error downloading from Google Drive: {str(e)}")
        return False


def download_from_url(url, output_path):
    """Download file from any direct URL into output_path."""
    try:
        logger.info(f"Downloading from URL: {url}")
        response = requests.get(url, stream=True, timeout=60)

        if response.status_code == 200:
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            logger.info(f"File downloaded successfully: {output_path}")
            return True

        logger.error(f"Failed to download file. Status code: {response.status_code}")
        return False

    except Exception as e:
        logger.error(f"Error downloading from URL: {str(e)}")
        return False


def _download_any(url, path):
    """Helper: download from Drive or direct URL based on hostname."""
    if "drive.google.com" in url.lower():
        return download_from_gdrive(url, path)
    return download_from_url(url, path)


# =========================
# Routes
# =========================

@app.route('/', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "service": "Audio Mixer API",
        "version": "1.3 (fixed intro/outro + safe crossfades)",
        "defaults": {
            "beginning_audio_url": BEGINNING_AUDIO_URL,
            "ending_audio_url": ENDING_AUDIO_URL,
            "beginning_volume": DEFAULT_BEGINNING_VOLUME,
            "ending_volume": DEFAULT_ENDING_VOLUME,
            "gap_before_ms": DEFAULT_GAP_BEFORE_MS,
            "gap_after_ms": DEFAULT_GAP_AFTER_MS,
            "crossfade_intro_ms": DEFAULT_CROSSFADE_INTRO_MS,
            "crossfade_outro_ms": DEFAULT_CROSSFADE_OUTRO_MS
        }
    })


@app.route('/mix-audio', methods=['POST'])
def mix_audio():
    """
    Mix voice audio with background music, with fixed intro/outro defaults.
    You may still override any default by passing it in the JSON request.

    Required:
      - voice_audio_url

    Optional (overrides):
      - voice_volume, background_volume, output_format
      - beginning_audio_url, ending_audio_url
      - beginning_volume, ending_volume
      - gap_before_ms, gap_after_ms
      - crossfade_intro_ms, crossfade_outro_ms
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        # Required
        voice_audio_url = data.get('voice_audio_url')
        if not voice_audio_url:
            return jsonify({"error": "voice_audio_url is required"}), 400

        # Levels / output (with sensible defaults)
        voice_volume = data.get('voice_volume', DEFAULT_VOICE_VOLUME)
        background_volume = data.get('background_volume', DEFAULT_BACKGROUND_VOLUME)
        output_format = data.get('output_format', DEFAULT_OUTPUT_FORMAT)

        # Intro/outro URLs (use fixed defaults unless overridden)
        beginning_audio_url = _normalize_optional_url(
            data.get('beginning_audio_url', BEGINNING_AUDIO_URL)
        )
        ending_audio_url = _normalize_optional_url(
            data.get('ending_audio_url', ENDING_AUDIO_URL)
        )

        # Volumes (defaults to your fixed values)
        beginning_volume = data.get('beginning_volume', DEFAULT_BEGINNING_VOLUME)
        ending_volume = data.get('ending_volume', DEFAULT_ENDING_VOLUME)

        # Gaps & crossfades (defaults to your fixed values)
        gap_before_ms = int(data.get('gap_before_ms', DEFAULT_GAP_BEFORE_MS))
        gap_after_ms = int(data.get('gap_after_ms', DEFAULT_GAP_AFTER_MS))
        crossfade_intro_ms = int(data.get('crossfade_intro_ms', DEFAULT_CROSSFADE_INTRO_MS))
        crossfade_outro_ms = int(data.get('crossfade_outro_ms', DEFAULT_CROSSFADE_OUTRO_MS))

        logger.info(
            f"Processing mix: voice={voice_audio_url}, intro={beginning_audio_url}, "
            f"outro={ending_audio_url}"
        )

        # Temp workspace
        temp_dir = tempfile.mkdtemp()
        unique_id = str(uuid.uuid4())

        voice_file = os.path.join(temp_dir, f"voice_{unique_id}.bin")
        bg_music_file = os.path.join(temp_dir, f"background_{unique_id}.bin")
        beginning_file = os.path.join(temp_dir, f"beginning_{unique_id}.bin")
        ending_file = os.path.join(temp_dir, f"ending_{unique_id}.bin")
        output_file = os.path.join(temp_dir, f"mixed_{unique_id}.{output_format}")

        created_files = []

        try:
            # --- Download inputs ---
            if not _download_any(voice_audio_url, voice_file):
                return jsonify({"error": "Failed to download voice audio"}), 400
            created_files.append(voice_file)

            if not _download_any(BACKGROUND_MUSIC_URL, bg_music_file):
                return jsonify({"error": "Failed to download background music"}), 500
            created_files.append(bg_music_file)

            beginning = None
            if beginning_audio_url:
                if not _download_any(beginning_audio_url, beginning_file):
                    return jsonify({"error": "Failed to download beginning (intro) audio"}), 400
                created_files.append(beginning_file)

            ending = None
            if ending_audio_url:
                if not _download_any(ending_audio_url, ending_file):
                    return jsonify({"error": "Failed to download ending (outro) audio"}), 400
                created_files.append(ending_file)

            # --- Load audio ---
            voice = AudioSegment.from_file(voice_file)
            background = AudioSegment.from_file(bg_music_file)
            if beginning_audio_url:
                beginning = AudioSegment.from_file(beginning_file)
            if ending_audio_url:
                ending = AudioSegment.from_file(ending_file)

            # --- Adjust levels ---
            voice = voice + voice_volume
            background = background + background_volume
            if beginning is not None:
                beginning = beginning + beginning_volume
            if ending is not None:
                ending = ending + ending_volume

            # --- Prepare background to match voice duration (loop and trim) ---
            if len(background) < len(voice):
                loops_needed = len(voice) // len(background) + 1
                background = background * loops_needed
            background = background[:len(voice)]

            # --- Overlay voice on background (main mix) ---
            mixed = voice.overlay(background)

            # --- Compose full program safely (intro/gaps/outro) ---
            program = mixed

            # Intro
            if beginning is not None:
                if crossfade_intro_ms > 0:
                    cf_intro = max(0, min(crossfade_intro_ms, len(beginning) - 1, len(program) - 1))
                    if cf_intro > 0:
                        program = beginning.append(program, crossfade=cf_intro)
                    else:
                        program = beginning + program
                else:
                    # hard cut with optional gap
                    if gap_before_ms > 0:
                        program = beginning + AudioSegment.silent(duration=gap_before_ms) + program
                    else:
                        program = beginning + program

            # Outro
            if ending is not None:
                if crossfade_outro_ms > 0:
                    # Crossfade directly from program into outro (avoid fading a tiny silence)
                    cf_outro = max(0, min(crossfade_outro_ms, len(program) - 1, len(ending) - 1))
                    if cf_outro > 0:
                        program = program.append(ending, crossfade=cf_outro)
                    else:
                        program = program + ending
                    # Optional silence AFTER the outro (not part of the crossfade)
                    if gap_after_ms > 0:
                        program = program + AudioSegment.silent(duration=gap_after_ms)
                else:
                    # hard cut with optional gap BEFORE the outro
                    if gap_after_ms > 0:
                        program = program + AudioSegment.silent(duration=gap_after_ms)
                    program = program + ending

            final_audio = program

            # --- Export ---
            export_kwargs = {}
            if output_format.lower() in ("mp3", "aac"):
                export_kwargs["bitrate"] = "128k"

            final_audio.export(output_file, format=output_format, **export_kwargs)

            with open(output_file, 'rb') as f:
                mixed_audio_data = f.read()

            # --- Cleanup ---
            for fp in created_files + [output_file]:
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                except Exception:
                    pass
            try:
                os.rmdir(temp_dir)
            except Exception:
                pass

            logger.info("Audio mixing (with fixed intro/outro) completed successfully")

            return send_file(
                io.BytesIO(mixed_audio_data),
                mimetype=f'audio/{output_format}',
                as_attachment=True,
                download_name=f'mixed_audio_{unique_id}.{output_format}'
            )

        except Exception as e:
            # Cleanup on error
            for fp in created_files:
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                except Exception:
                    pass
            try:
                if os.path.exists(temp_dir):
                    os.rmdir(temp_dir)
            except Exception:
                pass
            logger.exception("Error during mix pipeline")
            return jsonify({"error": f"Processing error: {str(e)}"}), 500

    except Exception as e:
        logger.error(f"Error in mix_audio: {str(e)}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


@app.route('/mix-audio-url', methods=['POST'])
def mix_audio_return_url():
    """Mix audio and return a temporary URL (alternative endpoint)."""
    try:
        return mix_audio()
    except Exception as e:
        logger.error(f"Error in mix_audio_return_url: {str(e)}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
