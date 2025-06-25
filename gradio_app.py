import gradio as gr
from openai import OpenAI
import time
import random
import os
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient
import uuid

JST = timezone(timedelta(hours=9))

load_dotenv()

# Mongo 起動を待ってから接続
mongo_client = MongoClient(os.getenv("MONGO_URL"))

# データベースとコレクションを定義
quiz_generator_db = mongo_client["db_quiz_generator"]
exercise_col = quiz_generator_db["exercises"]
history_col = quiz_generator_db["history"]
logs_col = quiz_generator_db["logs"]

def load_session_info(request: gr.Request):
    lti = request.session['user']
    return lti

def handle_answer(user_id, session, contents_id, page, no, user_answer, result, evaluation, report_type, report_text):
    report = {
        "report_type": report_type,
        "report_text": report_text
    }
    history_doc = {
        "user": user_id,
        "contents_id": contents_id,
        "page": page,
        "no": no,
        "user_answer": user_answer,
        "result": result,
        "evaluation": evaluation,
        "report": report,
        "timestamp": datetime.now(JST),
        "session_id": session
    }
    history_col.insert_one(history_doc)
    return

def handle_exercise(save, no, quiz_text, standard_answer, user, exercise_creation_time, answer_creation_time, model, session, rubric=None, figure_explanation=""):
    if save:
        # rubricがNoneなら空にする
        rubric = rubric or {}

        # 保存対象の小問構造
        new_entry = {
            "contents_id": "ai_generated",
            "page": user,
            "no": no,
            "quiz_text": quiz_text,
            "standard_answer": standard_answer,
            "figure_explanation": figure_explanation,
            "rubric": rubric,
            "exercise_creation_time": exercise_creation_time,
            "answer_creation_time": answer_creation_time,
            "creation_model": model,
            "session_id": session,
            "show": True
        }
        exercise_col.insert_one(new_entry)
    return

def handle_logs(user_id, operationname, session, value=None):
    logs_col.insert_one({
        "user": user_id,
        "session_id": session,
        "timestamp": datetime.now(JST),
        "operationname": operationname,
        "value": value
    })
    return

# ✅ MongoDBから再読み込みして State と Dropdown を更新する関数
def reload_quiz_map_from_mongo():
    documents = list(exercise_col.find({}))  # ← 全件取得

    if not documents:
        return {}, {}, gr.update(choices=[], value=None)

    # quiz_text_dict = quiz_text → (contentid, page, no)
    quiz_text_dict = {}

    for doc in documents:
        quiz_text = doc.get("quiz_text")
        contents_id = doc.get("contents_id")
        page = str(doc.get("page"))
        no = str(doc.get("no"))

        if quiz_text and contents_id and page and no:
            quiz_text_dict[quiz_text] = (contents_id, page, no)
    return quiz_text_dict, gr.update(choices=list(quiz_text_dict.keys()), value=None)

phrases = [
    "がんばるでありMath！",
    "自信を持って解きMath！",
    "あきらめないで、まだ解けMath！",
    "できる気がしてきMath！",
    "その一問が未来を変えMath！",
    "君の数的センス、輝いていMath！",
    "難問？ いや、やればできMath！",
    "悩んだ分だけ、伸びていきMath！",
    "ステップアップの数式は、君の中にありMath！",
    "失敗しても大丈夫、それも経験値に変えMath！",
    "つまずいた数だけ、君は強くなりMath！",
    "どんな問題も、君なら解けMath！"
]

# userのこれまでのresultを入手する
def get_result_from_db(contents_id, page, no, user):
    # MongoDBクエリ
    query = {
        "user": user,
        "contents_id": contents_id,
        "page": page,
        "no": no
    }

    # 条件に一致する全ドキュメント取得
    matching_docs = list(history_col.find(query))

    if not matching_docs:
        return 0, None  # 該当なし

    # タイムスタンプ順で最新の1件を取得
    def parse_ts(doc):
        ts = doc.get("timestamp")
        if isinstance(ts, datetime):
            return ts
        try:
            return datetime.fromisoformat(ts)
        except:
            return datetime.min

    latest_doc = max(matching_docs, key=parse_ts)

    return len(matching_docs), latest_doc.get("result")

reason = ["この問題についてはよくできています。さらに知識を応用した問題で復習しましょう！\n",
          "最後の最後でミスをしています。最後まで気を抜かずに、しっかり解き切りましょう！\n",
          "途中までよくできています。元の問題を解くために必要なステップを確認するために、以下の問題に取り組みましょう！\n",
          "説明がところどころ間違っているようです。怪しいポイントを確認して、カンペキに解けるようになりましょう！\n",
          "ちょっと難しすぎましたね。でも大丈夫。ひとつずつ確認しましょう。\n"]

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
models = ["gpt-3.5-turbo", "gpt-4-turbo", "gpt-4o", "gpt-4o-mini", "o1-preview", "o1-mini", "o3-mini"]

# 測定したい関数
def gpt_exection(model, query):
    completion = openai_client.chat.completions.create(
        model=model,
        messages=[
            {
            "role": "user", "content": query
            }
        ]
    ) 
    return completion.choices[0].message.content

def classify_binary(binary_string, knowledge):
    if all(c == '1' for c in binary_string):  # すべて1の場合
        text = """生徒がこの問題で分からなかった部分はないので、この問題に使われている知識に別の知識を組み合わせた新しい問題を作ってください。"""
        return text, 0
    elif all(c == '0' for c in binary_string):  # すべて0の場合
        text = """生徒はこの問題が全くわかっていないので、この問題に使われている最初の知識を簡単に復習する問題を作ってください。"""
        return text, 4
    elif binary_string.endswith('0') and all(c == '1' for c in binary_string[:-1]):  # 最後だけが0の場合
        text = "生徒は最後の最後でミスをしているので、最後まで解き切らせるような問題を作ってください。"
        return text, 1
    elif '01' not in binary_string:  # 1が左、0が右（交互にならない）
        text = "生徒は問題の「{}」から「{}」までの知識はわかっているので、「{}」から「{}」までの知識を扱う問題を作成してください。".format(knowledge[0], knowledge[binary_string.index('0')], knowledge[0], knowledge[binary_string.index('0') + 1])
        return text, 2
    else:  # 1と0が交互の場合、最初の0の位置を返す
        text = "生徒はところどころ理解していない部分があるので、「{}」を確認する問題を作成してください。".format(knowledge[binary_string.index('0') + 1])
        return text, 3

def check_if_solvable(question, knowledge, num, prev_exercise, prev_ans, model):
    if num == "type1":
        prompt = '''
        以下はある数学の問題です。 \n {} \n
        この問題を解いて、最終的な答えを出しなさい。
        - 以下の知識を用いても良い。 \n {} \n
        - 以下のフォーマットのように**$$で囲み**mathjax形式で出力すること。
        - ただし、mathjaxフォーマットの&は使わないこと。
        例：$$\\int_{}^{} f(x)\\\\,dx = F(b) - F(a)$$
        例：$$\\beta + \\gamma \\{} \\\\ \\alpha \\{}$$
        - 解答の過程を出力すること。
        - 以下のフォーマットで、XXXXに解答の過程、YYYYに最終的な答えを挿入して答えること。
        [解答の過程] \n
        $$ \\begin{}{}{} XXXX \\end{}{} $$
        [最終的な答え] \n
        $$YYYY$$
        '''.format(question, knowledge, r"{a}", r"{b}", r"text{の値から、}", r"text{を求める}", "{array", "}l{", "}", "{array", "}")
        print(prompt)
    count = 0
    start_time_solve1 = time.time()
    while True:
        count += 1
        ans = gpt_exection(model, prompt)
        end_time_solve1 = time.time()
        elapsed_time = end_time_solve1 - start_time_solve1
        if ("[解答の過程]" in ans) and ("[最終的な答え]" in ans):
            break
        elif count == 2:
            print("error_check_if_solvable")
            return "error_check_if_solvable"
    print(num + ":" + str(count))
    return ans

def execute0006_ks(question, answer, knowledge, tags, model):
    stats_bit = ''.join(str(1 if tag == '_o_' else 0) for tag in tags)
    stats, bittype = classify_binary(stats_bit, knowledge)
    prompt = '''
    生徒がある問題を解きました。問題に必要な数学的思考・計算技術・注意すべき点は次の通りです。 \n {} \n
    {} \n
    新しい問題のみを結果として出力すること。
    問題は以下のフォーマットのように**$$で囲み**mathjax形式で出力すること。ただし、mathjaxフォーマットの&は使わないこと。
    例：$$\\int_{}^{} f(x)\\\\,dx = F(b) - F(a)$$
    例：$$\\beta + \\gamma \\{} \\\\ \\alpha \\{}$$
    以下のフォーマットで、XXXXに問題を挿入して、左揃えで答えること。
    [問題] \n
    $$ \\begin{}{}{} XXXX \\end{}{} $$
    '''.format(knowledge, stats, r"{a}", r"{b}", r"text{の値から、}", r"text{を求める}", "{array", "}l{", "}", "{array", "}")

    count = 0
    start_time_all = time.time()
    while True:
        count += 1
        if count == 5:
            return "error", "error"
        start_time = time.time()
        ans = gpt_exection(model, prompt)
        end_time = time.time()
        elapsed_time_creation = end_time - start_time
        start_time = time.time()
        if "[問題]" in ans:
            new_exercise_answer1 = check_if_solvable(ans, knowledge, "type1", question, answer, model)
            solver = new_exercise_answer1
            end_time = time.time()
            elapsed_time_solve = end_time - start_time
            break
    end_time_all = time.time()
    elapsed_time_all = end_time_all - start_time_all
    return reason[bittype], ans, f"{elapsed_time_creation:.2f}", solver, f"{elapsed_time_solve:.2f}", f"{elapsed_time_all:.2f}"
    # return reason[bittype] + "\n" + ans + f"\n問題生成時間: {elapsed_time_creation:.2f}秒", solver + f"\n解答生成時間: {elapsed_time_solve:.2f}秒" + f"\n(全体実行時間: {elapsed_time_all:.2f}秒)"
    # return reason[bittype] + "\n" + "sample_question", "sample_answer"

# rubricの説明を抽出する関数
def get_main_explanations(quiz_text, quiz_text_dict):
    contents_id, page, no = quiz_text_dict[quiz_text]
    exercise_info = exercise_col.find_one({"contents_id": contents_id, "page": page, "no": no})
    rubrics = exercise_info.get("rubric", {})
    main_list = [item["main"] for item in rubrics.values() if "main" in item]

    return main_list

def initial_register():
    sample_problem = {
        "contents_id": "sample_001",
        "page": "1",
        "no": "1",
        "quiz_title": "二次方程式の解と係数の関係",
        "quiz_text": "(1-1) 二次方程式x^2+(a+2)x+2a=0の2つの異なる解α,βについて,α(α-1)＋β(β-1)=12である。この時,aの値を求めなさい。",
        "standard_answer": "[解答の過程] \\n x^2+(a+2)x+2a=0の2つの異なる解α,βは、解と係数の関係からα+β=-a-2, αβ=2aを満たす。 \\n また、α(α-1)＋β(β-1)=α^2-α+β^2-β=α^2+β^2-α-β=(α+β)^2-2αβ-(α+β)=(-a-2)^2-2 \\times 2a-(-a-2)=a^2+a+6 \\n よって、a^2+a+6=12 \\n (a-2)(a+3)=0 より、a=2, -3 \\n a=2のとき、方程式はx^2+4x+4=0 これを解くとx=-2より、異なる解α,βと矛盾するため、不適。 \\n a=-3のとき、方程式はx^2-x-6=0 これを解くとx=-2, 3 こちらは題意を満たす。 \\n よって、a=-3 \\n [最終的な解答] \\n a=-3",
        "figure_explanation": "",
        "rubric": {
            "1": {
                "main": "解と係数の関係を用いて、αβ、α＋βをaを用いて表そうとしている。",
                "example": {
                    "1": "「解と係数の関係」を表す言葉",
                    "2": "α+β=-a-2, αβ=2aという式"
                }
            },
            "2": {
                "main": "α(α-1)＋β(β-1)=12を用いて、aを求める二次方程式が導出されている。",
                "example": {
                    "1": "式変形をして、α(α-1)＋β(β-1)=a^2+a-6",
                    "2": "方程式を連立する"
                }
            },
            "3": {
                "main": "aの候補が導出できている。",
                "example": {
                    "1": "a=2, -3",
                    "2": "aの候補を求める"
                }
            },
            "4": {
                "main": "二つのaの値が、それぞれ条件を満たすか吟味する記述がある。",
                "example": {
                    "1": "解の吟味をする",
                    "2": "a=2のとき、α,βは異なる解にならず、矛盾する。",
                    "3": "a=2のとき、方程式を解くとx=-2となり、これは重解であるから、矛盾する。",
                    "4": "a=2のとき、方程式を解くとx=-2となるから、矛盾する。"
                }
            },
            "5": {
                "main": "a=-3と導出している。",
                "example": {
                    "1": "a=-3"
                }
            }
        },
        "show": True
    }

    # 追加または置き換え（重複を避けたい場合）
    exercise_col.replace_one({"contents_id": "sample_001", "page": "1", "no":"1"}, sample_problem, upsert=True)

    sample_history = {
        "user": "imachauy",
        "contents_id": "sample_001",
        "page": "1",
        "no": "1",
        "user_answer": "",
        "result": "すべて自力で解けた",
        "evaluation": "",
        "report": {
            "report_type": "",
            "report_text": ""
        },
        "timestamp": datetime.now(JST),
        "session_id": "sample_001"
    }

    # 登録
    history_col.replace_one({"session_id": "sample_001"}, sample_history, upsert=True)
    return

with gr.Blocks() as demo:
    lti_state = gr.State()  # ここにユーザー情報を保存

    user_info_output = gr.Markdown()
    
    user_state = gr.State("imachauy")
    session_state = gr.State()
    operationname_state = gr.State()
    contentsid_state = gr.State()
    page_state = gr.State()
    no_state = gr.State()
    quiz_map_state = gr.State()
    exercise_saving_state = gr.State(True)

    gr.Markdown(
        """
        <div style="background-color: #fff2a8; padding: 24px; border-radius: 8px; text-align: center;">
        <h1> 復 習 し M a t h </h1>
        </div>
        """
    )
    
    report_result = gr.Markdown(
        "## 正常に送信されました！ 次も...！",
        visible=False
    )
    
    with gr.Row():
        with gr.Column(scale=5): 
            title = gr.Markdown(
                "## " + random.choice(phrases),
                visible=True
            )

        with gr.Column(scale=1): 
            vanish_btn = gr.Button(
                value="タイトルを消す",
                visible=True,
                interactive=True,
                variant="secondary"
            )
    title_state = gr.State()

    quiz_dropdown = gr.Dropdown(
        choices=[],
        label="問題を選んでください",
        value=None
    )
    dropdown_state = gr.State()

    quiz_text_display = gr.Markdown(visible=False)

    with gr.Row():  
        with gr.Column(scale=1):    
            status_msg = gr.Markdown(
                value='',
                visible=False
            )

        with gr.Column(scale=1):
            checkboxes = gr.CheckboxGroup(
                choices=[], 
                label="できたポイントをチェックしよう", 
                visible=False, 
                show_label = True
            )
    status_msg_state = gr.State()
    checkbox_state = gr.State()
    checkbox_all_items_state = gr.State()
    
    gen_quiz_btn = gr.Button("復習問題を作成する（まだ押せません）", visible=True, interactive=False, variant="stop")

    with gr.Row():
        with gr.Column(scale=1):
            exercise_output = gr.Markdown(
                value="復習問題はここに出てきます",
                visible=True
            )

        with gr.Column(scale=1):
            student_answer = gr.Textbox(
                label="生成問題の解答を書いてみよう",
                lines=1,
                placeholder="(まだ入力できません)",
                visible=True,
                interactive=False
            )
    exercise_state = gr.State()
    exercise_creation_time_state = gr.State()
    answer_creation_time_state = gr.State()
    overall_creation_time_state = gr.State()
    new_contentsid_state = gr.State()
    new_page_state = gr.State()
    new_no_state = gr.State()
    model_state = gr.State("o3-mini")

    answer_btn = gr.Button("模範解答を表示", visible=False)

    with gr.Row():
        with gr.Column(scale=2):
            answer_output = gr.Markdown(
                "",
                visible=False
            )
        answer_state = gr.State()

        with gr.Column(scale=1):
            understanding = gr.Radio(
                choices=["すべて自力で解けた", "解説を見てわかった", "解説を見てもわからなかった"],
                label="この問題はどの程度理解できましたか？",
                visible=False
            )

            difficulty = gr.Radio(
                choices=["難しかった", "ちょうどよかった", "簡単だった"],
                label="この問題は難しかったですか？",
                visible=False
            )

            relevance = gr.Radio(
                choices=["関連していた", "関連していなかった"],
                label="この問題はもとの問題と関連していましたか？",
                visible=False
            )

            fluency = gr.Radio(
                choices=["自然だった", "不自然だった"],
                label="この問題の問題文は自然な日本語でしたか？",
                visible=False
            )

            rating = gr.Radio(
                choices=["復習になった", "復習にならなかった"],
                label="この問題は復習の役に立ちましたか？",
                visible=False
            )

            report_markdown = gr.Markdown(
                "この問題はみんなに共有されます（あなたの名前は共有されません）。言葉でみんなに伝えたいことがあれば、以下から報告できます。",
                visible=False
            )

            report_type = gr.Radio(
            choices=["模範解答が間違ってるかも？", "わからない場所がある...", "こうすればより良い問題になる", "その他"],
            label="報告の種類を選んでください",
            visible=False,
            interactive=True
            )

            report_text = gr.Textbox(
                label="こちらに詳しく記述してください",
                placeholder="(報告の種類を選ぶまでは書けません)",
                visible=False,
                interactive=False
            )
        understanding_state = gr.State()
        rating_state = gr.State()
        report_type_state = gr.State()
        report_text_state = gr.State()
    
    note_mkdwn = gr.Markdown(
       """
       <div style="text-align: center;">
       下のボタンは、理解度セルフチェックと問題の評価に答えたあとに押せます。<br>
       まずは自分の理解度を確認しましょう。
       </div>
       """,
       visible=False
    )

    with gr.Row():
        with gr.Column(scale=1):
            report_btn = gr.Button(
                interactive=False, 
                value="結果を送信して、別の復習用問題を解く", 
                variant="secondary",
                visible=False
            )
        
        with gr.Column(scale=1):
            report_btn_1 = gr.Button(
                interactive=False, 
                value="結果を送信して、元の問題を解く", 
                variant="secondary",
                visible=False
            )
        
        with gr.Column(scale=1):
            report_btn_2 = gr.Button(
                interactive=False, 
                value="結果を送信して、今の問題を解き直す", 
                variant="secondary",
                visible=False
            )

    vanish_btn.click(
        fn=lambda: (
            gr.update(visible=False),
            gr.update(visible=False),
            "VanishedTitle"
        ),
        inputs=None,
        outputs=[title, vanish_btn, operationname_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, title],
        outputs=None
    )

    def generate_status_msg(count, result_text):
        color_map = {
            "まったくわからなかった": "red",
            "解説を見てもわからなかった": "red",
            "すべて自力で解けた": "green",
            "一部解説を見て解いた": "orange",
            "解説を見てわかった": "orange",
            "正解": "green",
            "不正解": "red"
        }
        color = color_map.get(result_text, "black")

        if result_text:
            html = f"""
            <div style="text-align: center;">
                あなたは、この問題を <span style="font-weight: bold;">{count}回解きました</span> <br><br>
                そして、最後に<br>
                <span style="font-size: 28px; font-weight: bold; color: {color};">
                    {result_text}
                </span><br>
                と答えました。<br><br>
                <span style="font-weight: bold; color: gray;"> まずはセルフチェックをしましょう！ </span><br><br>
                右の項目から、自分が理解している部分にチェックを入れましょう。<br>
                一つも理解していなければ、チェックをしなくても良いです。
            </div>
            """
        else:
            html = f"""
            <div style="text-align: center;">
                あなたが解いたデータが見つかりませんでした。<br>
                まずはセルフチェックをしましょう。<br><br>
                右の項目から、自分が理解している部分にチェックを入れましょう。<br>
                一つもわからなければ、チェックをしなくても良いです。
            </div>
            """
        return html

    def update_when_dropdown(quiz_text, quiz_text_dict, user):
        rubric_explanations = get_main_explanations(quiz_text, quiz_text_dict)
        contents_id, page, no = quiz_text_dict[quiz_text]
        count, result_text = get_result_from_db(contents_id, page, no, user)
        msg = generate_status_msg(count, result_text)

        return (
            gr.update(value=f"### 問題文\n{quiz_text}"),
            gr.update(choices=rubric_explanations, value=[], visible=True, interactive=True),
            gr.update(interactive=True, variant="stop", value="復習問題を作成する"),
            gr.update(visible=True, value=msg),
            quiz_text,
            "SelectedExercise",
            rubric_explanations,
            contents_id,
            page,
            no
        )

    quiz_dropdown.change(
        fn=update_when_dropdown,
        inputs=[quiz_dropdown, quiz_map_state, user_state],
        outputs=[
            quiz_text_display, 
            checkboxes, 
            gen_quiz_btn,
            status_msg,
            dropdown_state,
            operationname_state,
            checkbox_all_items_state,
            contentsid_state,
            page_state,
            no_state
        ]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, dropdown_state],
        outputs=None
    )

    def update_when_checkboxes(all_items, selected):
        # 選択結果を "o" / "x" で辞書化
        result = {
            choice: "o" if choice in selected else "x"
            for choice in all_items
        }
        return result
    
    checkboxes.change(
        fn=update_when_checkboxes,
        inputs=[checkbox_all_items_state, checkboxes],
        outputs=checkbox_state
    ).then(
        fn=lambda: (
        "SelectedRubricStatus"
        ),
        inputs=None,
        outputs=[operationname_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, checkbox_state],
        outputs=None
    )

    def update_when_gen_quiz_btn(quiz_text, selections, quiz_text_dict, model):
        rubrics = get_main_explanations(quiz_text, quiz_text_dict)
        selected = selections or []
        tags = ['_o_' if item in selected else '_x_' for item in rubrics]

        contents_id, page, no = quiz_text_dict[quiz_text]
        exercise_info = exercise_col.find_one({"contents_id": contents_id, "page": page, "no": no})
        standard_answer = exercise_info.get("standard_answer", "")

        reason, new_exercise, exercise_creation_time, new_answer, answer_creation_time, overall_creation_time = execute0006_ks(quiz_text, standard_answer, rubrics, tags, model)

        return (
            new_exercise, 
            new_answer,
            exercise_creation_time,
            answer_creation_time,
            overall_creation_time, 
            gr.update(value=reason + "\n" + new_exercise + f"\n問題生成時間:" + exercise_creation_time + "秒"), 
            gr.update(visible=True, variant="primary", interactive=True),
            gr.update(placeholder="ここに記述してください", visible=True, interactive=True, lines=10)
        )

    gen_quiz_btn.click(
        fn=lambda: (
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(interactive=False),
        "SubmittedCheck"
        ),
        inputs=None,
        outputs=[quiz_dropdown, checkboxes, gen_quiz_btn, operationname_state]
    ).then(
        fn=update_when_gen_quiz_btn,
        inputs=[quiz_dropdown, checkboxes, quiz_map_state, model_state],
        outputs=[exercise_state, 
                 answer_state, 
                 exercise_creation_time_state, 
                 answer_creation_time_state,
                 overall_creation_time_state,
                 exercise_output, 
                 answer_btn, 
                 student_answer]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, checkbox_state],
        outputs=None
    )

    def update_when_answer_btn(solver, answer_time, overall_time):
        return solver + f"\n解答生成時間: {answer_time}秒" + f"\n(全体実行時間: {overall_time}秒)"
    
    answer_btn.click(
        fn=update_when_answer_btn,
        inputs=[answer_state, answer_creation_time_state, overall_creation_time_state],
        outputs=answer_output,
    ).then(
        fn=lambda: (
        gr.update(visible=True),
        gr.update(visible=True, interactive=True),
        gr.update(visible=True, interactive=True),
        gr.update(visible=True, interactive=True),
        gr.update(visible=True, interactive=True),
        gr.update(visible=True, interactive=True),
        gr.update(visible=True, interactive=False),
        gr.update(visible=True, interactive=True),
        gr.update(visible=True),
        gr.update(visible=True),
        gr.update(visible=True, interactive=False),
        gr.update(visible=True, interactive=False),
        gr.update(visible=True, interactive=False),
        gr.update(visible=True, interactive=False),
        gr.update(visible=True),
        "AnsweredExercise"
        ),
        inputs=None,
        outputs=[answer_output, 
                 understanding,
                 difficulty,
                 fluency,
                 relevance, 
                 rating, 
                 answer_btn, 
                 report_type, 
                 report_text, 
                 report_markdown, 
                 report_btn, 
                 report_btn_1, 
                 report_btn_2, 
                 student_answer, 
                 note_mkdwn, 
                 operationname_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, student_answer],
        outputs=None
    )

    def enable_submit(understanding_val, rating_val, difficulty_val, fluency_val, relevance_val):
        if (understanding_val is not None) and (rating_val is not None) and (difficulty_val is not None) and (fluency_val is not None) and (relevance_val is not None):
            return gr.update(interactive=True, value="結果を送信して、別の復習用問題を解く", variant="primary"), gr.update(interactive=True, value="結果を送信して、元の問題を解く", variant="primary"), gr.update(interactive=True, value="結果を送信して、今の問題を解き直す", variant="primary")
        else:
            return gr.update(interactive=False, value="結果を送信して、別の復習用問題を解く", variant="secondary"), gr.update(interactive=False, value="結果を送信して、元の問題を解く", variant="secondary"), gr.update(interactive=False, value="結果を送信して、今の問題を解き直す", variant="secondary")

    understanding.change(
        fn=enable_submit,
        inputs=[understanding, rating, difficulty, fluency, relevance],
        outputs=[report_btn, report_btn_1, report_btn_2]
    ).then(
        fn=lambda: ("SelectedComprehensibility"),
        inputs=None,
        outputs=[operationname_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, understanding],
        outputs=None
    )

    rating.change(
        fn=enable_submit,
        inputs=[understanding, rating, difficulty, fluency, relevance],
        outputs=[report_btn, report_btn_1, report_btn_2]
    ).then(
        fn=lambda: ("SelectedUsefulness"),
        inputs=None,
        outputs=[operationname_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, rating],
        outputs=None
    )

    difficulty.change(
        fn=enable_submit,
        inputs=[understanding, rating, difficulty, fluency, relevance],
        outputs=[report_btn, report_btn_1, report_btn_2]
    ).then(
        fn=lambda: ("SelectedDifficulty"),
        inputs=None,
        outputs=[operationname_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, rating],
        outputs=None
    )

    fluency.change(
        fn=enable_submit,
        inputs=[understanding, rating, difficulty, fluency, relevance],
        outputs=[report_btn, report_btn_1, report_btn_2]
    ).then(
        fn=lambda: ("SelectedFluency"),
        inputs=None,
        outputs=[operationname_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, rating],
        outputs=None
    )

    relevance.change(
        fn=enable_submit,
        inputs=[understanding, rating, difficulty, fluency, relevance],
        outputs=[report_btn, report_btn_1, report_btn_2]
    ).then(
        fn=lambda: ("SelectedRelevance"),
        inputs=None,
        outputs=[operationname_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, rating],
        outputs=None
    )

    report_type.change(
        fn=lambda _: gr.update(placeholder="例：式が間違っている気がします、〜の部分がわかりません、など。なるべく具体的に", interactive=True, lines=10),
        inputs=report_type,
        outputs=report_text
    ).then(
        fn=lambda: ("SelectedReportType"),
        inputs=None,
        outputs=[operationname_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, report_type],
        outputs=None
    )
    
    def get_next_no_for_user(user_name, save, contents_id, page, no):
        if save:
            # 該当ユーザのレコードを全件取得
            records = list(exercise_col.find({
                "contents_id": "ai_generated",
                "page": user_name
            }))

            if len(records)==0:
                return "ai_generated", user_name, "1"  # 該当がなければ1からスタート

            # no を整数に変換して最大値を探す
            max_no = max(int(record.get("no", 0)) for record in records)

            return "ai_generated", user_name, str(max_no + 1)
        else:
            return contents_id, page, no

    # 送信ボタンクリックで「送信しました」と表示
    report_btn.click(
        fn=get_next_no_for_user,
        inputs=[user_state, exercise_saving_state, contentsid_state, page_state, no_state],
        outputs=[new_contentsid_state, new_page_state, new_no_state]
    ).then(
        fn=handle_exercise,
        inputs=[exercise_saving_state, new_no_state, exercise_state, answer_state, user_state, exercise_creation_time_state, answer_creation_time_state, model_state, session_state],
        outputs=None
    ).then(
        fn=handle_answer,
        inputs=[user_state, session_state, new_contentsid_state, new_page_state, new_no_state, student_answer, understanding, rating, report_type, report_text],
        outputs=None
    ).then(
        fn=lambda: (
        "ReportedtoSolveAnotherProblem"
        ),
        inputs=None,
        outputs=[operationname_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state, report_type],
        outputs=None
    ).then(
        fn=lambda: (
            gr.update(visible=True),
            gr.update(visible=True, interactive=True, value=None),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=True, interactive=False),
            gr.update(value="復習問題はここに出てきます"),
            gr.update(visible=True, interactive=False, placeholder="(まだ入力できません)", value="", lines=1),
            gr.update(visible=False, interactive=False),
            gr.update(visible=False, value=""),
            gr.update(visible=False, interactive=False, value=None),
            gr.update(visible=False, interactive=False, value=None),
            gr.update(visible=False, interactive=False, value=None),
            gr.update(visible=False, interactive=False, value=None),
            gr.update(visible=False, interactive=False, value=None),
            gr.update(visible=False),
            gr.update(visible=False, interactive=False, value=None),
            gr.update(visible=False, interactive=False, placeholder="(報告の種類を選ぶまでは書けません)", value="", lines=1),
            gr.update(visible=False, interactive=False, value="", variant="secondary"),
            gr.update(visible=False, interactive=False, value="", variant="secondary"),
            gr.update(visible=False, interactive=False, value="", variant="secondary"),
            gr.update(value="## " + random.choice(phrases)),
            gr.update(visible=False),
            True
        ),
        inputs=None,
        outputs=[
            report_result,
            quiz_dropdown,
            quiz_text_display,
            status_msg,
            checkboxes,
            gen_quiz_btn,
            exercise_output,
            student_answer,
            answer_btn,
            answer_output,
            understanding,
            difficulty,
            fluency,
            relevance,
            rating,
            report_markdown,
            report_type,
            report_text,
            report_btn,
            report_btn_1,
            report_btn_2,
            title,
            note_mkdwn,
            exercise_saving_state
        ]
    ).then(
        fn=reload_quiz_map_from_mongo,
        inputs=None,
        outputs=[quiz_map_state, quiz_dropdown]
    ).then(
        fn=lambda: (
        "StartedSession",
        str(uuid.uuid4())
        ),
        inputs=None,
        outputs=[operationname_state, session_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state],
        outputs=None
    )

    # ✅ 初回自動読み込み（表示直後に1回実行）
    demo.load(
        fn=load_session_info,
        inputs=None,
        outputs=[lti_state]
    ).then(
        fn=initial_register,
        inputs=None,
        outputs=None
    ).then(
        fn=reload_quiz_map_from_mongo,
        inputs=None,
        outputs=[quiz_map_state, quiz_dropdown]
    ).then(
        fn=lambda: (
        "StartedSession",
        str(uuid.uuid4())
        ),
        inputs=None,
        outputs=[operationname_state, session_state]
    ).then(
        fn=handle_logs,
        inputs=[user_state, operationname_state, session_state],
        outputs=None
    )
