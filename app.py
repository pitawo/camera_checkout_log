from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_socketio import SocketIO, emit
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, date
import json
import os
import atexit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'camera-ledger-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*")


# --- 日付ユーティリティ ---
def parse_date(date_str):
    """'1/15' や '2026/1/15' 形式の日付をdateオブジェクトに変換"""
    if not date_str:
        return None
    try:
        parts = date_str.strip().split('/')
        if len(parts) == 2:
            # '1/15' 形式 → 今年か来年
            month, day = int(parts[0]), int(parts[1])
            year = date.today().year
            result = date(year, month, day)
            # 過去の日付なら来年と判断
            if result < date.today():
                result = date(year + 1, month, day)
            return result
        elif len(parts) == 3:
            # '2026/1/15' 形式
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            return date(year, month, day)
    except:
        pass
    return None

def format_date_for_display(d):
    """dateオブジェクトを '1/15' 形式に変換"""
    if isinstance(d, str):
        d = parse_date(d)
    if d:
        return f"{d.month}/{d.day}"
    return ""

def format_date_for_storage(d):
    """dateオブジェクトを '2026/1/15' 形式に変換（保存用）"""
    if d:
        return f"{d.year}/{d.month}/{d.day}"
    return ""

def dates_overlap(start1, end1, start2, end2):
    """2つの期間が重複しているかチェック"""
    return start1 <= end2 and end1 >= start2

# データファイルのパス
DATA_FILE = os.path.join(os.path.dirname(__file__), 'camera_data.json')

# --- データの保存・読み込み ---
def save_data():
    """データをJSONファイルに保存"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] データを保存しました")
    except Exception as e:
        print(f"[ERROR] データ保存失敗: {e}")

def migrate_camera_data(cam):
    """古い形式のカメラデータを新形式に変換"""
    if "reservations" not in cam:
        cam["reservations"] = []
        # 既存の貸出情報を予約リストに移行
        if cam.get("status") == "貸出中" and cam.get("user"):
            # 古い period 形式 "1/15 ～ 1/20" をパース
            period = cam.get("period", "")
            start_str, end_str = "", ""
            if "～" in period:
                parts = period.split("～")
                start_str = parts[0].strip()
                end_str = parts[1].strip() if len(parts) > 1 else ""

            start_date = parse_date(start_str)
            end_date = parse_date(end_str)

            cam["reservations"].append({
                "user": cam.get("user", ""),
                "start_date": format_date_for_storage(start_date) if start_date else "",
                "end_date": format_date_for_storage(end_date) if end_date else "",
                "purpose": cam.get("purpose", "")
            })
        # 古いフィールドを削除
        cam.pop("user", None)
        cam.pop("period", None)
        cam.pop("purpose", None)
        cam.pop("status", None)  # statusは動的に計算するため削除

    # 予約データのマイグレーション（period形式 → start_date/end_date形式）
    for res in cam.get("reservations", []):
        if "period" in res and "start_date" not in res:
            period = res.get("period", "")
            start_str, end_str = "", ""
            if "～" in period:
                parts = period.split("～")
                start_str = parts[0].strip()
                end_str = parts[1].strip() if len(parts) > 1 else ""
            start_date = parse_date(start_str)
            end_date = parse_date(end_str)
            res["start_date"] = format_date_for_storage(start_date) if start_date else ""
            res["end_date"] = format_date_for_storage(end_date) if end_date else ""
            res.pop("period", None)
        res.pop("is_current", None)  # 不要になったフラグを削除

    return cam

def load_data():
    """JSONファイルからデータを読み込み"""
    global data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # データ形式をマイグレーション
            for cam in data["cameras"]:
                migrate_camera_data(cam)
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] データを読み込みました（{len(data['cameras'])}台）")
            return
        except Exception as e:
            print(f"[ERROR] データ読み込み失敗: {e}")

    # ファイルがない場合は初期データを使用
    data = {
        "cameras": [
            {"id": 1, "name": "Canon EOS R5", "reservations": []},
            {"id": 2, "name": "Sony α7IV", "reservations": []},
            {"id": 3, "name": "Nikon Z6II", "reservations": []},
            {"id": 4, "name": "GoPro Hero 11", "reservations": []},
            {"id": 5, "name": "DJI Ronin-S", "reservations": []},
        ]
    }
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 初期データを使用します")

# --- 擬似データベース ---
data = {}
load_data()

# --- スケジューラー設定 ---
scheduler = BackgroundScheduler(daemon=True)

# 20:59に保存（21:00シャットダウンの1分前）
scheduler.add_job(func=save_data, trigger='cron', hour=20, minute=59, id='save_before_shutdown')

# 6:59に保存（7:00起動後のバックアップ用）
scheduler.add_job(func=save_data, trigger='cron', hour=6, minute=59, id='save_morning_backup')

scheduler.start()

# アプリ終了時にスケジューラーを停止＆データ保存
atexit.register(lambda: scheduler.shutdown())
atexit.register(save_data)


def get_camera_status(cam):
    """カメラのステータスを動的に計算"""
    today = date.today()
    for res in cam.get("reservations", []):
        start = parse_date(res.get("start_date", ""))
        end = parse_date(res.get("end_date", ""))
        if start and end and start <= today <= end:
            return "貸出中", res  # 現在貸出中の予約情報も返す
    return "空き", None

def enrich_reservation(res):
    """予約データに表示用情報を追加"""
    today = date.today()
    start = parse_date(res.get("start_date", ""))
    end = parse_date(res.get("end_date", ""))

    # 表示用の期間文字列
    start_display = format_date_for_display(start) if start else ""
    end_display = format_date_for_display(end) if end else ""
    res["period_display"] = f"{start_display} ～ {end_display}"

    # キャンセル用に元の日付文字列を保持
    res["start_date"] = res.get("start_date", "")
    res["end_date"] = res.get("end_date", "")

    # ステータス判定
    if start and end:
        if today > end:
            res["status"] = "ended"  # 終了済み
        elif start <= today <= end:
            res["status"] = "current"  # 現在利用中
        else:
            res["status"] = "future"  # 将来の予約
    else:
        res["status"] = "unknown"

    return res

def get_all_data():
    """全データをJSON形式で取得（ステータスを動的計算）"""
    today = date.today()
    cameras_with_status = []

    for cam in data["cameras"]:
        status, current_res = get_camera_status(cam)

        # 予約を開始日でソートし、終了済みを除外
        valid_reservations = []
        for res in cam.get("reservations", []):
            enriched = enrich_reservation(res.copy())
            if enriched["status"] != "ended":  # 終了済みは表示しない
                valid_reservations.append(enriched)

        # 開始日でソート
        valid_reservations.sort(key=lambda r: parse_date(r.get("start_date", "")) or date.max)

        cam_data = {
            "id": cam["id"],
            "name": cam["name"],
            "status": status,
            "reservations": valid_reservations
        }
        cameras_with_status.append(cam_data)

    available_count = len([c for c in cameras_with_status if c["status"] == "空き"])
    total_count = len(cameras_with_status)
    busy_count = total_count - available_count

    return {
        "cameras": cameras_with_status,
        "available": available_count,
        "total": total_count,
        "busy": busy_count
    }

@app.route('/')
def index():
    all_data = get_all_data()
    error = request.args.get('error', '')
    return render_template('index.html', cameras=all_data["cameras"], available=all_data["available"], total=all_data["total"], busy=all_data["busy"], error=error)

@app.route('/api/data')
def api_data():
    """APIエンドポイント: 全データ取得"""
    return jsonify(get_all_data())

@app.route('/reserve', methods=['POST'])
def reserve():
    cam_id = int(request.form.get('cam_id'))
    user = request.form.get('user')
    start_date_str = request.form.get('start_date')
    end_date_str = request.form.get('end_date')
    purpose = request.form.get('purpose', '')

    # 日付をパース
    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)
    today = date.today()

    # バリデーション
    error = None
    if not start_date or not end_date:
        error = "日付の形式が正しくありません（例: 1/15）"
    elif start_date < today:
        error = "開始日は今日以降の日付を指定してください"
    elif end_date < start_date:
        error = "終了日は開始日以降の日付を指定してください"
    else:
        # 重複チェック
        for cam in data["cameras"]:
            if cam["id"] == cam_id:
                for res in cam.get("reservations", []):
                    res_start = parse_date(res.get("start_date", ""))
                    res_end = parse_date(res.get("end_date", ""))
                    if res_start and res_end and dates_overlap(start_date, end_date, res_start, res_end):
                        error = f"この期間は既に予約があります（{format_date_for_display(res_start)} ～ {format_date_for_display(res_end)}）"
                        break
                break

    if error:
        # エラーがある場合はJSONで返す（フロントでハンドリング）
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": error}), 400
        # 通常のフォーム送信の場合はクエリパラメータでエラーを渡す
        return redirect(url_for('index', error=error))

    # 予約を追加
    for cam in data["cameras"]:
        if cam["id"] == cam_id:
            reservation = {
                "user": user,
                "start_date": format_date_for_storage(start_date),
                "end_date": format_date_for_storage(end_date),
                "purpose": purpose
            }
            cam["reservations"].append(reservation)
            break

    save_data()
    socketio.emit('data_updated', get_all_data())
    return redirect(url_for('index'))

@app.route('/return/<int:cam_id>')
def return_cam(cam_id):
    today = date.today()
    for cam in data["cameras"]:
        if cam["id"] == cam_id:
            # 現在利用中の予約（今日が期間内）を削除
            new_reservations = []
            for res in cam.get("reservations", []):
                start = parse_date(res.get("start_date", ""))
                end = parse_date(res.get("end_date", ""))
                # 現在利用中でないものだけ残す
                if not (start and end and start <= today <= end):
                    new_reservations.append(res)
            cam["reservations"] = new_reservations
            break

    save_data()
    socketio.emit('data_updated', get_all_data())
    return redirect(url_for('index'))

@app.route('/cancel/<int:cam_id>')
def cancel_reservation(cam_id):
    """予約をキャンセル"""
    start_date_str = request.args.get('start')
    end_date_str = request.args.get('end')

    for cam in data["cameras"]:
        if cam["id"] == cam_id:
            # 指定された期間の予約を削除
            new_reservations = []
            for res in cam.get("reservations", []):
                if res.get("start_date") == start_date_str and res.get("end_date") == end_date_str:
                    continue  # この予約をスキップ（削除）
                new_reservations.append(res)
            cam["reservations"] = new_reservations
            break

    save_data()
    socketio.emit('data_updated', get_all_data())
    return redirect(url_for('index'))

@app.route('/settings', methods=['POST'])
def update_master():
    action = request.form.get('action')

    if action == 'add':
        # カメラ追加
        new_name = request.form.get('new_name', '').strip()
        if new_name:
            new_id = max([c["id"] for c in data["cameras"]], default=0) + 1
            data["cameras"].append({"id": new_id, "name": new_name, "reservations": []})

    elif action == 'delete':
        # カメラ削除
        delete_id = request.form.get('delete_id', '')
        if delete_id:
            cam_id = int(delete_id)
            # 貸出中でないもののみ削除
            new_cameras = []
            for c in data["cameras"]:
                if c["id"] != cam_id:
                    new_cameras.append(c)
                else:
                    status, _ = get_camera_status(c)
                    if status == "貸出中":
                        new_cameras.append(c)  # 貸出中なら削除しない
            data["cameras"] = new_cameras

    elif action == 'rename':
        # カメラ名変更
        rename_id = request.form.get('rename_id', '')
        rename_name = request.form.get('rename_name', '').strip()
        if rename_id and rename_name:
            cam_id = int(rename_id)
            for cam in data["cameras"]:
                if cam["id"] == cam_id:
                    cam["name"] = rename_name
                    break

    save_data()
    socketio.emit('data_updated', get_all_data())
    return redirect(url_for('index'))

# 手動保存エンドポイント（管理用）
@app.route('/api/save')
def manual_save():
    save_data()
    return {"status": "ok", "message": "データを保存しました"}

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, use_reloader=False)
