import streamlit as st
import sqlite3
from datetime import date, datetime
import uuid
import os
import re
import time

import stripe
from openai import OpenAI
from openai import RateLimitError, AuthenticationError


# ===============================
# ENV
# ===============================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "")  # 例: https://xxx.streamlit.app

client = OpenAI(api_key=OPENAI_API_KEY)

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# ===============================
# DB
# ===============================
conn = sqlite3.connect("menu_ai.db", check_same_thread=False)
cur = conn.cursor()


def ensure_table_schema():
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage (
        user_id TEXT,
        day TEXT,
        count INTEGER,
        PRIMARY KEY (user_id, day)
    )
    """)

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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        user_id TEXT PRIMARY KEY,
        stripe_customer_id TEXT,
        stripe_subscription_id TEXT,
        status TEXT,
        current_period_end INTEGER,
        cancel_at_period_end INTEGER,
        updated_at TEXT
    )
    """)
    conn.commit()


ensure_table_schema()


# ===============================
# uid（リロード維持）
# ===============================
qp = st.query_params
if "uid" in qp and qp["uid"]:
    user_id = qp["uid"]
else:
    user_id = str(uuid.uuid4())
    st.query_params["uid"] = user_id

today = str(date.today())


# ===============================
# usage
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


# ===============================
# history
# ===============================
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
# subscription DB
# ===============================
def upsert_subscription(uid, customer_id, sub_id, status, current_period_end, cancel_at_period_end):
    cur.execute("""
    INSERT INTO subscriptions (user_id, stripe_customer_id, stripe_subscription_id, status, current_period_end, cancel_at_period_end, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET
      stripe_customer_id=excluded.stripe_customer_id,
      stripe_subscription_id=excluded.stripe_subscription_id,
      status=excluded.status,
      current_period_end=excluded.current_period_end,
      cancel_at_period_end=excluded.cancel_at_period_end,
      updated_at=excluded.updated_at
    """, (
        uid,
        customer_id or "",
        sub_id or "",
        status or "",
        int(current_period_end) if current_period_end else 0,
        1 if cancel_at_period_end else 0,
        datetime.now().isoformat(timespec="seconds")
    ))
    conn.commit()


def get_subscription(uid):
    cur.execute("""
    SELECT stripe_customer_id, stripe_subscription_id, status, current_period_end, cancel_at_period_end
    FROM subscriptions
    WHERE user_id=?
    """, (uid,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "stripe_customer_id": row[0],
        "stripe_subscription_id": row[1],
        "status": row[2],
        "current_period_end": int(row[3] or 0),
        "cancel_at_period_end": bool(row[4] or 0),
    }


# ===============================
# Stripe: Checkout / 状態同期 / 解約
# ===============================
def create_checkout_session(uid: str):
    if not (STRIPE_SECRET_KEY and APP_BASE_URL and STRIPE_PRICE_ID):
        return None

    # ✅ ASCII URLのみ（日本語NG）
    success_url = f"{APP_BASE_URL}/?uid={uid}&success=1&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{APP_BASE_URL}/?uid={uid}&canceled=1"

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=uid,
        allow_promotion_codes=True,
    )
    return session.url


def handle_return_from_stripe(uid: str):
    """決済完了後、Stripe側のsession_idからsubscriptionをDBに保存"""
    if not STRIPE_SECRET_KEY:
        return

    if qp.get("success") == "1" and qp.get("session_id"):
        session_id = qp["session_id"]
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            sub_id = sess.get("subscription")
            customer_id = sess.get("customer")

            if sub_id:
                s = stripe.Subscription.retrieve(sub_id)
                status = s["status"]
                current_period_end = s.get("current_period_end", 0)
                cancel_at_period_end = bool(s.get("cancel_at_period_end", False))

                upsert_subscription(uid, customer_id, sub_id, status, current_period_end, cancel_at_period_end)
                st.success("✅ プレミアム登録が完了しました！")

                # ✅ URLを掃除（毎回success表示されるの防止）
                st.query_params["uid"] = uid
                st.rerun()

        except Exception as e:
            st.error(f"⚠️ Stripe確認に失敗しました: {e}")


def refresh_subscription_from_stripe(uid: str):
    """毎回Stripeを見に行って “今の状態” をDBへ同期（Webhook無しでもズレにくい）"""
    if not STRIPE_SECRET_KEY:
        return

    sub = get_subscription(uid)
    if not sub:
        return

    sub_id = sub.get("stripe_subscription_id")
    if not sub_id:
        return

    try:
        s = stripe.Subscription.retrieve(sub_id)
        status = s["status"]
        current_period_end = s.get("current_period_end", 0)
        customer_id = s.get("customer", "")
        cancel_at_period_end = bool(s.get("cancel_at_period_end", False))

        upsert_subscription(uid, customer_id, s["id"], status, current_period_end, cancel_at_period_end)
    except Exception:
        return


def is_premium(uid: str) -> bool:
    """最終判定（active/trialingならプレミアム扱い）"""
    sub = get_subscription(uid)
    if not sub:
        return False

    status = (sub["status"] or "").lower()
    now_ts = int(time.time())
    end_ts = int(sub["current_period_end"] or 0)

    # ✅ active / trialing ならOK（cancel予約してても期間内はOK）
    if status in ["active", "trialing"]:
        if end_ts == 0:
            return True
        return end_ts > now_ts

    return False


def cancel_subscription_at_period_end(uid: str):
    """✅ 解約予約（次回更新で停止）"""
    if not STRIPE_SECRET_KEY:
        return False, "STRIPE_SECRET_KEY未設定"

    sub = get_subscription(uid)
    if not sub or not sub.get("stripe_subscription_id"):
        return False, "subscription情報が見つかりません"

    try:
        sub_id = sub["stripe_subscription_id"]
        stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
        refresh_subscription_from_stripe(uid)
        return True, "解約予約しました（期限まではプレミアム利用できます）"
    except Exception as e:
        return False, f"Stripe解約予約エラー: {e}"


def cancel_subscription_immediately(uid: str):
    """⚠️ 今すぐ解約（即停止）"""
    if not STRIPE_SECRET_KEY:
        return False, "STRIPE_SECRET_KEY未設定"

    sub = get_subscription(uid)
    if not sub or not sub.get("stripe_subscription_id"):
        return False, "subscription情報が見つかりません"

    try:
        sub_id = sub["stripe_subscription_id"]
        stripe.Subscription.delete(sub_id)  # 即キャンセル
        refresh_subscription_from_stripe(uid)
        return True, "今すぐ解約しました"
    except Exception as e:
        return False, f"Stripe即解約エラー: {e}"


# ===============================
# helpers
# ===============================
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


# Stripe return & sync
if STRIPE_SECRET_KEY and APP_BASE_URL:
    handle_return_from_stripe(user_id)

# ✅ 重要：毎回Stripeから最新状態を同期（Webhook無しでも強い）
refresh_subscription_from_stripe(user_id)

premium = is_premium(user_id)


# ===============================
# Sidebar（課金UI + 解約）
# ===============================
with st.sidebar:
    st.markdown("## 💎 プレミアム（月300円）")
    st.caption("✅ 無制限 / ✅ 制限解除")

    sub = get_subscription(user_id)

    if premium:
        st.success("🌟 プレミアム有効")

        if sub:
            end_ts = sub.get("current_period_end", 0)
            if end_ts:
                end_date = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d")
                st.caption(f"次回更新/期限：{end_date}")

            if sub.get("cancel_at_period_end"):
                st.warning("⚠️ 解約予約済み（期限までは利用OK）")

        st.divider()

        st.markdown("### 解約")
        if st.button("✅ 解約予約（次回更新で停止）"):
            ok, msg = cancel_subscription_at_period_end(user_id)
            if ok:
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

        with st.expander("⚠️ 今すぐ解約（即停止）"):
            st.caption("※押すと即プレミアムが止まります（注意）")
            if st.button("🚨 今すぐ解約する"):
                ok, msg = cancel_subscription_immediately(user_id)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

    else:
        st.info("🆓 無料プラン")

        if not STRIPE_SECRET_KEY:
            st.warning("STRIPE_SECRET_KEY が未設定です")
        if not APP_BASE_URL:
            st.warning("APP_BASE_URL が未設定です")
        if not STRIPE_PRICE_ID:
            st.warning("STRIPE_PRICE_ID が未設定です")

        if STRIPE_SECRET_KEY and APP_BASE_URL and STRIPE_PRICE_ID:
            if st.button("プレミアムにする（月300円）"):
                url = create_checkout_session(user_id)
                if url:
                    st.link_button("Stripe決済ページを開く", url)
                else:
                    st.error("Checkout作成に失敗しました（設定を確認）")


# ===============================
# ✅ 無料制限（1日1回）
# ===============================
MAX_FREE_PER_DAY = 1
today_count = get_today_count(user_id, today)

if premium:
    st.success("🌟 プレミアム：無制限（回数制限なし / 日数制限なし）")
else:
    st.info(f"🆓 本日の利用回数：{today_count} / {MAX_FREE_PER_DAY}（無料は1日分まで）")
    if today_count >= MAX_FREE_PER_DAY:
        st.error("⚠️ 無料利用は1日1回までです（明日リセット）")
        st.stop()

st.markdown("---")


# ===============================
# 入力フォーム
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
# 実行
# ===============================
if run:
    if not OPENAI_API_KEY:
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
"""
        mode_name = "献立モード"

    with st.spinner("生成中…"):
        try:
            res = client.responses.create(
                model="gpt-4.1-mini",
                input=prompt,
                max_output_tokens=900
            )
            result = res.output_text
        except RateLimitError:
            st.error("⚠️ 混雑中です。少し待ってもう一回押してください。")
            st.stop()
        except AuthenticationError as e:
            st.error(f"⚠️ APIキーが無効です\n\n{e}")
            st.stop()
        except Exception as e:
            st.error(f"⚠️ エラーが発生しました\n\n{e}")
            st.stop()

    if not recipe_mode:
        result = trim_menu_days(result, int(days))

    # 無料だけ回数カウント
    if not premium:
        increment_count(user_id, today)

    save_history(
        user_id, mode_name, text_input, int(days), int(people), int(dishes),
        selected_meals, methods, int(calorie), result
    )

    st.subheader("📄 結果")
    st.text(result)

    st.subheader("🛒 買い物リスト（チェック）")
    day_items = parse_shopping_list(result)

    if not day_items:
        st.write("買い物リストが見つかりませんでした。")
    else:
        day_keys = [k for k in day_items.keys() if k.endswith("日目")]
        day_keys_sorted = sorted(day_keys, key=lambda x: int(x.replace("日目", ""))) if day_keys else []

        if day_keys_sorted:
            for day_key in day_keys_sorted:
                st.markdown(f"### {day_key}")
                items = uniq_keep_order(day_items.get(day_key, []))
                for idx, item in enumerate(items):
                    st.checkbox(item, key=f"shop_{day_key}_{idx}_{hash(item)}")
        else:
            items = uniq_keep_order(day_items.get("all", []))
            for idx, item in enumerate(items):
                st.checkbox(item, key=f"shop_all_{idx}_{hash(item)}")

    with st.expander("🕘 履歴（最新5件）"):
        rows = load_history(user_id, 5)
        if not rows:
            st.write("まだ履歴がありません。")
        else:
            for i, (created_at, mode, inp, res_text) in enumerate(rows, start=1):
                st.markdown(f"**{i}件目** `{created_at}`（{mode}）")
                st.caption(f"入力：{inp}")
                st.text(res_text)
                st.divider()











































































