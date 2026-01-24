import streamlit as st
import sqlite3
from datetime import date, datetime
import uuid
import os
import re

from openai import OpenAI
from openai import RateLimitError, AuthenticationError


# ===============================
# OpenAI
# ===============================
API_KEY = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=API_KEY)


# ===============================
# DB（回数制限 + 履歴）
# ===============================
conn = sqlite3.connect("menu_ai.db", check_same_thread=False)
cur = conn.cursor()


def ensure_table_schema():
    """既存DBがあっても壊れないように最低限のマイグレーションを行う"""

    # usage
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage (
        user_id TEXT,
        day TEXT,
        count INTEGER,
        PRIMARY KEY (user_id, day)
    )
    """)

    # history
    cur.execute("""
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        created_at TEXT,
        mode TEXT,
        input_text TEXT,
        days INTEGER,
        people INTEGER,
        dishes INTEGER,
        meals TEXT,
        methods TEXT,
        calorie INTEGER,
        result TEXT
    )
    """)

    # 既存テーブルに不足カラムがあっても落ちないようにする
    def add_col_if_missing(table, col, coltype="TEXT"):
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        if col not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")

    # history のカラム補完
    add_col_if_missing("history", "user_id", "TEXT")
    add_col_if_missing("history", "created_at", "TEXT")
    add_col_if_missing("history", "mode", "TEXT")
    add_col_if_missing("history", "input_text", "TEXT")
    add_col_if_missing("history", "days", "INTEGER")
    add_col_if_missing("history", "people", "INTEGER")
    add_col_if_missing("history", "dishes", "INTEGER")
    add_col_if_missing("history", "meals", "TEXT")
    add_col_if_missing("history", "methods", "TEXT")
    add_col_if_missing("history", "calorie", "INTEGER")
    add_col_if_missing("history", "result", "TEXT")

    conn.commit()


ensure_table_schema()


# ===============================
# ユーザーID（リロードでも維持）
# ✅ URLに uid を埋める → F5しても制限が戻りにくい
# ===============================
qp = st.query_params  # Streamlit 1.30+

if "uid" in qp and qp["uid"]:
    user_id = qp["uid"]
else:
    user_id = str(uuid.uuid4())
    st.query_params["uid"] = user_id

today = str(date.today())


# ===============================
# 利用回数管理
# ===============================
def get_today_count(uid, day):
    cur.execute("SELECT count FROM usage WHERE user_id=? AND day=?", (uid, day))
    row = cur.fetchone()
    return row[0] if row else 0


def increment_count(uid, day):
    count = get_today_count(uid, day)
    if count == 0:
        cur.execute("INSERT INTO usage (user_id, day, count) VALUES (?, ?, 1)", (uid, day))
    else:
        cur.execute("UPDATE usage SET count=? WHERE user_id=? AND day=?", (count + 1, uid, day))
    conn.commit()


def save_history(uid, mode, input_text, days, people, dishes, meals, methods, calorie, result):
    cur.execute("""
    INSERT INTO history (user_id, created_at, mode, input_text, days, people, dishes, meals, methods, calorie, result)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        uid,
        datetime.now().isoformat(timespec="seconds"),
        mode,
        input_text,
        days,
        people,
        dishes,
        ",".join(meals) if meals else "",
        ",".join(methods) if methods else "",
        calorie,
        result
    ))
    conn.commit()


def load_history(uid, limit=5):
    cur.execute("""
        SELECT created_at, mode, input_text, result
        FROM history
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
    """, (uid, limit))
    return cur.fetchall()


# ===============================
# 便利関数
# ===============================
def extract_first_dish_name(text: str) -> str:
    m = re.search(r"【料理名】\s*(.+)", text)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"・\s*([^\n：]+)", text)
    return m2.group(1).strip() if m2 else ""


def parse_shopping_list(result_text: str):
    shop_match = re.search(r"【買い物リスト】([\s\S]+)", result_text)
    if not shop_match:
        return None

    block = shop_match.group(1).strip()
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]

    day_pattern = re.compile(r"^(?P<day>\d+日目)[:：]\s*$")
    day_map = {}
    current_day = None

    for ln in lines:
        md = day_pattern.match(ln)
        if md:
            current_day = md.group("day")
            day_map.setdefault(current_day, [])
            continue

        item = ln.lstrip("・- ").strip()
        if not item:
            continue

        if current_day:
            day_map[current_day].append(item)
        else:
            day_map.setdefault("all", []).append(item)

    has_day = any(k.endswith("日目") for k in day_map.keys())
    if has_day:
        return day_map
    else:
        return {"all": day_map.get("all", [])}


def uniq_keep_order(items):
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def trim_menu_days(result_text: str, days: int) -> str:
    """AIが勝手に日数増やした時、【献立】だけ指定日数にカット"""
    if days <= 0:
        return result_text

    m = re.search(r"【献立】([\s\S]*?)(?=\n【材料】|\n【作り方】|\n【買い物リスト】|$)", result_text)
    if not m:
        return result_text

    menu_block = m.group(1)
    day_blocks = re.findall(r"(\d+日目：[\s\S]*?)(?=\n\d+日目：|$)", menu_block)
    if len(day_blocks) <= days:
        return result_text

    kept = "\n".join(day_blocks[:days]).strip()
    new_menu = f"\n{kept}\n"

    start, end = m.span(1)
    return result_text[:start] + new_menu + result_text[end:]


# ===============================
# UI
# ===============================
st.set_page_config(page_title="献立AI", layout="centered")

st.markdown("""
<style>
.block-container { padding-top: 1.5rem; padding-bottom: 2rem; max-width: 560px; }
h1, h2, h3 { font-family: "Noto Sans JP", sans-serif; }
.stButton>button {
  width: 100%;
  padding: 14px 16px;
  border-radius: 14px;
  font-size: 18px;
  font-weight: 700;
}
.card {
  background: #fff;
  border-radius: 18px;
  padding: 18px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.06);
}
</style>
""", unsafe_allow_html=True)

st.title("🍳 献立AI（Streamlit版）")
st.caption("✅ 食材＋条件で献立生成 / ✅ 料理名モードでレシピ確認")


# ===============================
# プレミアム（本番ではStripe判定に差し替え）
# 広告は削除済み
# ===============================
if "premium" not in st.session_state:
    st.session_state.premium = False

with st.sidebar:
    st.markdown("## 💎 プラン")
    st.checkbox("プレミアム（無制限）※テスト用", key="premium")
    st.caption("本番はStripe連携で自動判定に置き換え予定")

premium = st.session_state.premium


# ===============================
# 無料制限（あなたのルール）
# 無料：1日分まで + 1日3回まで
# 有料：無制限
# ===============================
MAX_FREE_PER_DAY = 3
today_count = get_today_count(user_id, today)

if premium:
    st.success("🌟 プレミアム：無制限")
else:
    st.info(f"🆓 本日の利用回数：{today_count} / {MAX_FREE_PER_DAY}（無料は1日分まで）")
    if today_count >= MAX_FREE_PER_DAY:
        st.error("⚠️ 無料利用は1日3回までです（明日リセット）")
        st.stop()


st.markdown("---")


# ===============================
# 入力フォーム（機能は減らさない）
# ===============================
with st.container():
    st.markdown('<div class="card">', unsafe_allow_html=True)

    recipe_mode = st.checkbox("料理名モード（料理名からレシピを見る）", key="recipe_mode")

    text_input = st.text_area(
        "入力",
        placeholder="例：卵 豆腐 キャベツ\n例：親子丼（料理名モード）",
        key="text_input"
    )

    col1, col2, col3 = st.columns(3)

    # ✅ 無料ユーザーは日数=1固定（UI上も1しか選べない）
    days_max = 7 if premium else 1
    with col1:
        days = st.number_input("日数", 1, days_max, 1, key="days")
        if not premium:
            st.caption("🆓 無料は1日分まで")

    with col2:
        people = st.number_input("人数", 1, 10, 1, key="people")

    with col3:
        dishes = st.number_input("品数/食", 1, 5, 1, key="dishes")

    calorie = st.number_input("1食あたりの目標カロリー（kcal）", 200, 1500, 600, key="calorie")

    st.subheader("🍽 食事の時間（チェック）")
    meal_cols = st.columns(3)
    with meal_cols[0]:
        meal_morning = st.checkbox("朝", value=False, key="meal_morning")
    with meal_cols[1]:
        meal_lunch = st.checkbox("昼", value=False, key="meal_lunch")
    with meal_cols[2]:
        meal_dinner = st.checkbox("夜", value=True, key="meal_dinner")

    selected_meals = []
    if meal_morning:
        selected_meals.append("朝")
    if meal_lunch:
        selected_meals.append("昼")
    if meal_dinner:
        selected_meals.append("夜")
    if not selected_meals:
        selected_meals = ["夜"]

    methods = st.multiselect(
        "調理条件",
        ["火を使わない", "洗い物少なめ", "簡単", "節約"],
        key="methods"
    )

    run = st.button("献立を作る", use_container_width=True)

    st.markdown("</div>", unsafe_allow_html=True)


# ===============================
# 実行処理
# ===============================
if run:
    if not API_KEY:
        st.error("⚠️ OPENAI_API_KEY が設定されていません（環境変数を確認）")
        st.stop()

    if not text_input.strip():
        st.warning("入力してください")
        st.stop()

    method_text = "、".join(methods) if methods else "なし"

    if recipe_mode:
        prompt = f"""
あなたは料理の先生です。

【料理名】
{text_input}

【条件】
・{people}人分（必ず守る）
・家庭料理
・初心者向け
・材料と作り方は短くわかりやすく
・現実的な材料のみ

【出力形式】
【料理名】
（料理名）

【材料】
・材料名 分量

【作り方】
1. 手順
2. 手順

【買い物リスト】
・材料名
"""
        mode_name = "料理名モード"

    else:
        prompt = f"""
あなたは一人暮らし向け献立アドバイザーです。

【入力食材】
{text_input}

【条件】
・日数：{days}日分（必ずこの日数だけ）
・人数：{people}人分
・食事の時間：{", ".join(selected_meals)}
・1食あたり：{dishes}品
・目標カロリー：{calorie}kcal
・調理条件：{method_text}

【絶対ルール】
・曜日（月曜など）は一切使わない
・「1日目」「2日目」…の日数表記にする
・{days}日分を超えない
・入力食材以外は絶対に追加しない（調味料は例外OK）
・各料理は「料理名 + 一言」も入れる

【出力形式（必ずこの形）】
【献立】
1日目：
{", ".join(selected_meals)}：
・料理名：一言
（1食あたり{dishes}品）

（必要な日数分だけ繰り返す）

【材料】
（料理ごとに）
・材料名 分量

【作り方】
（料理ごとに短く）
1. 手順
2. 手順

【買い物リスト】
1日目：
・材料
（必要な日数分だけ）
"""
        mode_name = "献立モード"

    with st.spinner("生成中…（10〜30秒くらいかかることがあります）"):
        try:
            res = client.responses.create(
                model="gpt-4.1-mini",
                input=prompt,
                max_output_tokens=900
            )
            result = res.output_text

        except RateLimitError:
            st.error("⚠️ 混雑中です（API制限）。少し待ってもう一回押してください。")
            st.stop()

        except AuthenticationError as e:
            st.error(f"⚠️ APIキーが違う/無効です\n\n{e}")
            st.stop()

        except Exception as e:
            st.error(f"⚠️ エラーが発生しました\n\n{e}")
            st.stop()

    # ✅ 1日なのに増えた時は削る（献立だけ）
    if not recipe_mode:
        result = trim_menu_days(result, int(days))

    # ✅ 成功した時だけ回数カウント（無料だけ）
    if not premium:
        increment_count(user_id, today)

    # ✅ 履歴保存
    save_history(
        user_id, mode_name, text_input, int(days), int(people), int(dishes),
        selected_meals, methods, int(calorie), result
    )

    # ===============================
    # 結果本文
    # ===============================
    st.subheader("📄 結果")
    st.text(result)

    # ===============================
    # 買い物リスト（チェック）
    # ===============================
    st.subheader("🛒 買い物リスト（チェック）")

    day_items = parse_shopping_list(result)
    if not day_items:
        st.write("買い物リストが見つかりませんでした。")
    else:
        day_keys = [k for k in day_items.keys() if k.endswith("日目")]
        day_keys_sorted = sorted(day_keys, key=lambda x: int(x.replace("日目", ""))) if day_keys else []

        if day_keys_sorted:
            for day_key in day_keys_sorted:
                # 指定日数より先は表示しない
                try:
                    if int(day_key.replace("日目", "")) > int(days):
                        continue
                except:
                    pass

                st.markdown(f"### {day_key}")
                items = uniq_keep_order(day_items.get(day_key, []))
                if not items:
                    st.caption("（なし）")
                    continue

                for idx, item in enumerate(items):
                    st.checkbox(item, key=f"shop_{day_key}_{idx}_{hash(item)}")
        else:
            items = uniq_keep_order(day_items.get("all", []))
            for idx, item in enumerate(items):
                st.checkbox(item, key=f"shop_all_{idx}_{hash(item)}")

    # ===============================
    # 履歴表示
    # ===============================
    with st.expander("🕘 履歴（最新5件）"):
        rows = load_history(user_id, 5)
        if not rows:
            st.write("まだ履歴がありません。")
        else:
            for i, (created_at, mode, inp, res_text) in enumerate(rows, start=1):
                st.markdown(f"**{i}件目**  `{created_at}`  （{mode}）")
                st.caption(f"入力：{inp}")
                st.text(res_text)
                st.divider()








































































