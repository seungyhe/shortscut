import os
import re
import json
import uuid
import subprocess
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template

import anthropic

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024

UPLOAD_DIR = Path("/tmp/uploads")
OUTPUT_DIR = Path("/tmp/outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

jobs = {}

SYSTEM_PROMPT = """당신은 유튜브 쇼츠 전문 편집자입니다. 롱폼 영상의 자막/스크립트 또는 영상 메타정보를 분석해서 쇼츠로 만들기 최적인 구간을 추출합니다.

분석 기준:
1. 훅(Hook): 첫 2~3초에 시청자를 멈추게 하는 강렬한 순간
2. 완결성: 짧아도 그 자체로 이해되는 스토리/정보
3. 감정: 놀람, 웃음, 감동, 공감 등 반응을 유발하는 순간
4. 정보 밀도: 짧은 시간에 실용적 가치가 높은 구간

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 순수 JSON만 출력하세요:
{
  "shorts": [
    {
      "id": 1,
      "title": "쇼츠 제목 (클릭 유도형)",
      "start_sec": 135,
      "end_sec": 190,
      "start_str": "00:02:15",
      "end_str": "00:03:10",
      "duration": "55초",
      "hook": "첫 3초 훅 문구",
      "reason": "이 구간을 선택한 이유 (알고리즘 관점 2~3문장)",
      "score": 92,
      "tags": ["#해시태그1", "#해시태그2", "#Shorts"],
      "script": "이 구간의 전체 자막 스크립트"
    }
  ],
  "summary": "영상 전체 요약 (2~3문장)",
  "total": 4
}

쇼츠는 최소 3개, 최대 5개. score는 0~100 쇼츠 적합도. start_sec/end_sec은 반드시 정수(초 단위)."""


def get_video_duration(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return None


def extract_audio_transcript(video_path):
    duration = get_video_duration(video_path)
    size_mb = os.path.getsize(video_path) / 1024 / 1024
    name = Path(video_path).stem
    dur_str = f"{int(duration//3600):02d}:{int((duration%3600)//60):02d}:{int(duration%60):02d}" if duration else "알 수 없음"
    return (
        f"영상 파일명: {name}\n"
        f"영상 길이: {dur_str} ({int(duration or 0)}초)\n"
        f"파일 크기: {size_mb:.1f}MB\n\n"
        f"[자막 파일 없이 영상만 업로드됨]\n"
        f"영상 제목과 길이를 바탕으로 쇼츠 구간을 추천해주세요. "
        f"영상 길이가 {int(duration or 300)}초임을 고려해서 균등하게 분산된 구간을 추출하세요."
    )


def analyze_with_claude(content: str) -> dict:
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"다음 내용을 분석해서 쇼츠 구간을 추출해주세요:\n\n{content}"}]
    )
    raw = message.content[0].text
    clean = re.sub(r'```json|```', '', raw).strip()
    return json.loads(clean)


def cut_short(video_path: str, short: dict, output_path: str, job_id: str, idx: int):
    start = int(short["start_sec"])
    end = int(short["end_sec"])
    duration = end - start
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(video_path),
        "-t", str(duration),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path)
    ]
    jobs[job_id]["log"].append(f"[{idx+1}] FFmpeg 시작: {short['start_str']} ~ {short['end_str']}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode == 0:
        jobs[job_id]["log"].append(f"[{idx+1}] ✅ 완료: {Path(output_path).name}")
        return True
    else:
        jobs[job_id]["log"].append(f"[{idx+1}] ❌ 실패")
        return False


def process_job(job_id: str, video_path: str, subtitle_content: str):
    try:
        jobs[job_id]["status"] = "analyzing"
        jobs[job_id]["log"].append("Claude AI로 쇼츠 구간 분석 중...")

        if subtitle_content:
            content = subtitle_content
        else:
            content = extract_audio_transcript(video_path)

        analysis = analyze_with_claude(content)
        jobs[job_id]["analysis"] = analysis
        jobs[job_id]["log"].append(f"✅ 분석 완료: {len(analysis['shorts'])}개 구간 발견")

        if video_path and os.path.exists(video_path):
            jobs[job_id]["status"] = "cutting"
            jobs[job_id]["log"].append("영상 자르는 중...")
            job_out_dir = OUTPUT_DIR / job_id
            job_out_dir.mkdir(exist_ok=True)
            output_files = []
            for i, short in enumerate(analysis["shorts"]):
                safe_title = re.sub(r'[^\w가-힣\s]', '', short['title'])[:30].strip()
                out_name = f"shorts_{i+1}_{safe_title}.mp4"
                out_path = job_out_dir / out_name
                success = cut_short(video_path, short, str(out_path), job_id, i)
                if success:
                    output_files.append({
                        "index": i + 1,
                        "filename": out_name,
                        "path": str(out_path),
                        "title": short["title"],
                        "duration": short["duration"],
                        "score": short["score"],
                        "download_url": f"/download/{job_id}/{out_name}"
                    })
            jobs[job_id]["output_files"] = output_files
        else:
            jobs[job_id]["output_files"] = []
            jobs[job_id]["log"].append("영상 파일이 없어 분석 결과만 제공됩니다.")

        jobs[job_id]["status"] = "done"
        jobs[job_id]["log"].append("🎉 모든 작업 완료!")

    except json.JSONDecodeError as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = f"AI 응답 파싱 실패: {str(e)}"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["log"].append(f"❌ 오류: {str(e)}")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "queued", "log": [], "analysis": None, "output_files": [], "error": None}

    video_path = None
    subtitle_content = ""

    if "video" in request.files:
        video = request.files["video"]
        if video.filename:
            ext = Path(video.filename).suffix
            video_path = str(UPLOAD_DIR / f"{job_id}_video{ext}")
            video.save(video_path)
            jobs[job_id]["log"].append(f"영상 업로드 완료: {video.filename}")

    if "subtitle" in request.files:
        sub = request.files["subtitle"]
        if sub.filename:
            subtitle_content = sub.read().decode("utf-8", errors="ignore")
            jobs[job_id]["log"].append(f"자막 파일 로드: {sub.filename}")

    text_input = request.form.get("text_input", "").strip()
    if text_input and not subtitle_content:
        subtitle_content = text_input
        jobs[job_id]["log"].append(f"텍스트 입력 수신 ({len(text_input)}자)")

    if not video_path and not subtitle_content:
        return jsonify({"error": "영상 또는 자막/텍스트를 입력해주세요."}), 400

    t = threading.Thread(target=process_job, args=(job_id, video_path or "", subtitle_content))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    if job_id not in jobs:
        return jsonify({"error": "작업을 찾을 수 없습니다."}), 404
    job = jobs[job_id]
    return jsonify({
        "status": job["status"],
        "log": job["log"],
        "analysis": job["analysis"],
        "output_files": job["output_files"],
        "error": job["error"]
    })


@app.route("/download/<job_id>/<filename>")
def download(job_id, filename):
    path = OUTPUT_DIR / job_id / filename
    if not path.exists():
        return jsonify({"error": "파일을 찾을 수 없습니다."}), 404
    return send_file(str(path), as_attachment=True, download_name=filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
