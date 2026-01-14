import streamlit as st
from openai import OpenAI
import os
import re
from collections import defaultdict

# =====================
# 初期設定
# =====================
st.set_page_config(
    page_title="献立AI",
    page_icon="🍳",
    layout="centered"
)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

if "history" not in st.session_state:
    st.session_state.history = []

# =====================
# ヘルパー関数
# =====================
def extract_blocks(text):
    """
    【料理名】〜【作り方】を1料理ずつ分解
    """
    pattern = re.compile(
        r"【料理名】\s*(.*?)\n【材料】\n(.*?)\n【作り方】\n(.*?)(?=\n【料理名】|\Z)",
        re.S
    )
    return pattern.findall(text)

def multiply_amount(amount, people):
    """
    分量 × 人数（ざっくり）
    """
    m = re.search(r"(\d+)", amount)
    if m:
        num = int(m.group(1)) * people
        return re.sub(r"\d+", str(num), amount, 1)
    return amount

# =====================
# UI
# =====================
st.title("🍳 献立AI（Streamlit版）")

recipe_mode = st.checkbox("🍽 料理名モード（料理名 → レシピ）")

text_input = st.text_area(
    "入力",
    placeholder="例：卵 豆腐 キャベツ\n例：親子丼（料理名モード）"
)

col1, col2, col3 = st.columns(3)
days = col1.number_input("日数", 1, 7, 1)
people = col2.number_input("人数", 1, 10, 1)
dishes = col3.number_input("品数/食", 1, 3, 1)

meals = st.multiselect(
    "食事区分",
    ["朝", "昼", "夜"],
    default=["夜"]
)

conditions = st.multiselect(
    "条件",
    ["火を使わない", "洗い物少なめ", "簡単", "節約"]
)

# =====================
# 実行
# =====================
if st.button("生成する"):
    if not text_input.strip():
        st.warning("入力してください")
        st.stop()

    with st.spinner("生成中…"):
        try:
            # ========= プロンプト =========
            if recipe_mode:
                prompt = f"""
料理名:{text_input}
条件:
・1人分
・家庭料理
・短く簡潔

出力形式:
【料理名】
【材料】
・材料 分量
【作り方】
1.
2.
"""
            else:
                prompt = f"""
使う食材:{text_input}

条件:
・{days}日分
・{people}人分
・{'・'.join(meals)}
・1食{dishes}品
・制約:{'、'.join(conditions) if conditions else 'なし'}
・使っていない食材は出さない

出力形式（繰り返し）:
【料理名】
【材料】
・材料 分量
【作り方】
1.
2.
"""

            res = client.responses.create(
                model="gpt-4.1-mini",
                input=prompt,
                max_output_tokens=800
            )

            result = res.output_text
            st.session_state.history.append(result)

            # =====================
            # 表示（カードUI）
            # =====================
            st.markdown("## 🍽 献立")

            blocks = extract_blocks(result)
            shopping = defaultdict(str)

            for name, materials, steps in blocks:
                st.markdown(f"### 🍳 {name}")
                st.image(
                    f"https://source.unsplash.com/600x400/?{name}",
                    use_column_width=True
                )

                with st.expander("材料・作り方"):
                    st.markdown("**【材料】**")
                    for line in materials.splitlines():
                        if "・" in line:
                            item = line.replace("・", "").strip()
                            parts = item.split(" ", 1)
                            if len(parts) == 2:
                                mat, amt = parts
                                shopping[mat] = multiply_amount(amt, people)
                                st.write(f"・{mat} {multiply_amount(amt, people)}")
                            else:
                                st.write(f"・{item}")

                    st.markdown("**【作り方】**")
                    for s in steps.splitlines():
                        st.write(s)

                st.divider()

            # =====================
            # 買い物リスト
            # =====================
            st.markdown("## 🛒 買い物リスト（合算）")
            for mat, amt in shopping.items():
                st.checkbox(f"{mat} {amt}")

        except Exception as e:
            st.error(f"⚠️ エラーが発生しました\n{e}")

# =====================
# 履歴
# =====================
st.markdown("---")
st.markdown("## 🕘 履歴")

for i, h in enumerate(reversed(st.session_state.history[-5:]), 1):
    with st.expander(f"{i}件目"):
        st.write(h)
