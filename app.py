from flask import Flask, request, jsonify, send_file
import requests
import os
import tempfile
import subprocess
import io
from urllib.parse import urlparse, parse_qs
import uuid
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Fixed background music URL
BACKGROUND_MUSIC_URL = "https://drive.google.com/file/d/1Q-wkYSYpyR9_vC0DCvJr0wYAmgyVg5lJ/view?usp=sharing"

def download_from_gdrive(share_url, output_path):
    """Download file from Google Drive sharing URL"""
    try:
        # Extract file ID from the sharing URL
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
        
        # Create direct download URL
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        
        logger.info(f"Downloading from Google Drive: {file_id}")
        
        # Download the file
        response = requests.get(download_url, stream=True, timeout=60)
        
        # Handle Google Drive's virus scan warning for large files
        if response.status_code == 200:
            if 'virus scan warning' in response.text.lower() or 'download anyway' in response.text.lower():
                import re
                for line in response.text.split('\n'):
                    if 'download_warning' in line and 'href' in line:
                        confirm_url = re.search(r'href="([^"]*)"', line)
                        if confirm_url:
                            confirm_url = confirm_url.group(1).replace('&amp;', '&')
                            response = requests.get(f"https://drive.google.com{confirm_url}", stream=True, timeout=60)
                            break
        
        if response.status_code == 200:
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            logger.info(f"File downloaded successfully: {output_path}")
            return True
        else:
            logger.error(f"Failed to download file. Status code: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"Error downloading from Google Drive: {str(e)}")
        return False

def download_from_url(url, output_path):
    """Download file from any URL"""
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
        else:
            logger.error(f"Failed to download file. Status code: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"Error downloading from URL: {str(e)}")
        return False

def mix_audio_with_ffmpeg(voice_file, bg_file, output_file, voice_vol=0, bg_vol=-12):
    """Mix audio using FFmpeg directly"""
    try:
        # FFmpeg command to mix audio
        cmd = [
            'ffmpeg', '-y',  # -y to overwrite output file
            '-i', voice_file,     # Input 1: voice
            '-i', bg_file,        # Input 2: background music
            '-filter_complex', 
            f'[0:a]volume={voice_vol}dB[voice];[1:a]volume={bg_vol}dB,aloop=loop=-1:size=2e+09[bg];[voice][bg]amix=inputs=2:duration=first[out]',
            '-map', '[out]',
            '-c:a', 'mp3',
            '-b:a', '128k',
            output_file
        ]
        
        logger.info(f"Running FFmpeg command: {' '.join(cmd)}")
        
        # Run FFmpeg
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        if result.returncode == 0:
            logger.info("FFmpeg mixing completed successfully")
            return True
        else:
            logger.error(f"FFmpeg error: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg process timed out")
        return False
    except Exception as e:
        logger.error(f"Error running FFmpeg: {str(e)}")
        return False

@app.route('/', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "Audio Mixer API (FFmpeg)",
        "version": "2.0"
    })

@app.route('/mix-audio', methods=['POST'])
def mix_audio():
    """Mix voice audio with background music using FFmpeg"""
    try:
        # Get JSON data from request
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400
        
        # Get voice audio URL from request
        voice_audio_url = data.get('voice_audio_url')
        
        if not voice_audio_url:
            return jsonify({"error": "voice_audio_url is required"}), 400
        
        # Optional parameters
        voice_volume = data.get('voice_volume', 0)  # 0dB = no change
        background_volume = data.get('background_volume', -12)  # -12dB = ~25% volume
        output_format = data.get('output_format', 'mp3')
        
        logger.info(f"Processing audio mix request with voice URL: {voice_audio_url}")
        
        # Create temporary directory for processing
        temp_dir = tempfile.mkdtemp()
        unique_id = str(uuid.uuid4())
        
        voice_file = os.path.join(temp_dir, f"voice_{unique_id}.mp3")
        bg_music_file = os.path.join(temp_dir, f"background_{unique_id}.mp3")
        output_file = os.path.join(temp_dir, f"mixed_{unique_id}.mp3")
        
        try:
            # Download voice audio
            logger.info("Downloading voice audio...")
            if "drive.google.com" in voice_audio_url:
                voice_success = download_from_gdrive(voice_audio_url, voice_file)
            else:
                voice_success = download_from_url(voice_audio_url, voice_file)
            
            if not voice_success:
                return jsonify({"error": "Failed to download voice audio"}), 400
            
            # Download background music
            logger.info("Downloading background music...")
            bg_success = download_from_gdrive(BACKGROUND_MUSIC_URL, bg_music_file)
            
            if not bg_success:
                return jsonify({"error": "Failed to download background music"}), 500
            
            # Mix audio using FFmpeg
            logger.info("Mixing audio with FFmpeg...")
            mix_success = mix_audio_with_ffmpeg(
                voice_file, bg_music_file, output_file, 
                voice_volume, background_volume
            )
            
            if not mix_success:
                return jsonify({"error": "Failed to mix audio"}), 500
            
            # Read the mixed file for response
            with open(output_file, 'rb') as f:
                mixed_audio_data = f.read()
            
            # Clean up temporary files
            for temp_file in [voice_file, bg_music_file, output_file]:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            os.rmdir(temp_dir)
            
            logger.info("Audio mixing completed successfully")
            
            # Return the mixed audio file
            return send_file(
                io.BytesIO(mixed_audio_data),
                mimetype=f'audio/{output_format}',
                as_attachment=True,
                download_name=f'mixed_audio_{unique_id}.{output_format}'
            )
            
        except Exception as e:
            # Clean up on error
            for temp_file in [voice_file, bg_music_file, output_file]:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            if os.path.exists(temp_dir):
                os.rmdir(temp_dir)
            raise e
            
    except Exception as e:
        logger.error(f"Error in mix_audio: {str(e)}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
