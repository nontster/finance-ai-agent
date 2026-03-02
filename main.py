import os
import re
import sys
import operator
import httpx
from typing import TypedDict, Annotated, Sequence
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, END

# ==========================================
# 🚨 HACK: บังคับปิดการตรวจสอบ SSL (Bypass) สำหรับ Workshop
# ==========================================
original_client_init = httpx.Client.__init__

def unverified_client_init(self, *args, **kwargs):
    kwargs['verify'] = False # บังคับปิดการตรวจสอบ Certificate
    original_client_init(self, *args, **kwargs)

httpx.Client.__init__ = unverified_client_init

# ปิด Warning ที่จะแจ้งเตือนว่าเรากำลังเชื่อมต่อแบบไม่ปลอดภัย
import warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")
# ==========================================


# ==========================================
# 1. API Key Check & LLM Setup
# ==========================================
# The system will automatically look for the GOOGLE_API_KEY in the environment.
if not os.environ.get("GOOGLE_API_KEY"):
    print("❌ Error: GOOGLE_API_KEY environment variable is not set.")
    print("Please set it in your macOS terminal by running:")
    print("export GOOGLE_API_KEY='your_api_key_here'")
    sys.exit(1)

llm = ChatGoogleGenerativeAI(model="gemini-3-flash-preview", temperature=0)

# ==========================================
# 2. Define Memory (State) Structure
# ==========================================
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add] # Chat history
    user_profile: dict # Financial data storage (Memory)
    route_to: str # Variable to dictate the next agent in the workflow

# Structure to force the Guardrail LLM to output a specific JSON format
class IntentClassification(BaseModel):
    intent: str = Field(description="Question category: 'tax', 'retirement', or 'out_of_scope'")

# ==========================================
# 3. Deterministic Tools (Thai Progressive Tax)
# ==========================================
def calculate_tax_logic(income: float) -> float:
    """คำนวณภาษีเงินได้บุคคลธรรมดาตามอัตราก้าวหน้าของประเทศไทย"""
    
    # พิมพ์ Debug Message เพื่อแสดงว่า Tool ถูกเรียกใช้งาน
    print(f"\n[Debug: 🧮 Tool 'calculate_tax_logic' ถูกเรียกใช้งาน | รายได้เริ่มต้น: {income:,.2f} บาท]")
    
    # 1. หักค่าใช้จ่ายเหมาสูงสุด (100,000) และลดหย่อนส่วนตัว (60,000)
    # เพื่อแปลงรายได้ทั้งปี เป็น "เงินได้สุทธิ"
    net_income = max(0, income - 160000)
    print(f"[Debug: 🧮 เงินได้สุทธิหลังหักลดหย่อนพื้นฐาน: {net_income:,.2f} บาท]")
    
    tax = 0.0
    
    # 2. คำนวณภาษีตามฐานอัตราก้าวหน้าของไทย (ไล่จากบนลงล่าง)
    if net_income > 5000000:
        tax += (net_income - 5000000) * 0.35
        net_income = 5000000
    if net_income > 2000000:
        tax += (net_income - 2000000) * 0.30
        net_income = 2000000
    if net_income > 1000000:
        tax += (net_income - 1000000) * 0.25
        net_income = 1000000
    if net_income > 750000:
        tax += (net_income - 750000) * 0.20
        net_income = 750000
    if net_income > 500000:
        tax += (net_income - 500000) * 0.15
        net_income = 500000
    if net_income > 300000:
        tax += (net_income - 300000) * 0.10
        net_income = 300000
    if net_income > 150000:
        tax += (net_income - 150000) * 0.05
        # ส่วนที่ต่ำกว่า 150,000 ยกเว้นภาษี (0%)

    print(f"[Debug: 🧮 คำนวณเสร็จสิ้น | ภาษีที่ต้องจ่าย: {tax:,.2f} บาท]\n")
    return tax

# ==========================================
# 4. Create Nodes (Agent Functions)
# ==========================================
def guardrail_node(state: AgentState):
    """ตรวจสอบขอบเขต"""
    print("\n[System: 🛡️ Guardrail is checking intent...]")
    last_message = state["messages"][-1].content
    structured_llm = llm.with_structured_output(IntentClassification)
    
    prompt = f"""
    Analyze the user's message and categorize it strictly into one of these 3 categories:
    1. 'tax' : If asking about tax calculation, deductions.
    2. 'retirement' : If asking about retirement planning.
    3. 'out_of_scope' : If it is about anything else.
    
    User message: {last_message}
    """
    result = structured_llm.invoke(prompt)
    return {"route_to": result.intent}

def fallback_node(state: AgentState):
    """ตอบเมื่อหลุด Scope"""
    print("[System: 🛑 Routing to Fallback Node]")
    msg = AIMessage(content="ขออภัยครับ ขอบเขตของผมดูแลเฉพาะเรื่อง **การคำนวณภาษี** และ **การวางแผนเกษียณ** เท่านั้นครับ")
    return {"messages": [msg], "route_to": "end"} # ล้างค่าไม้ผลัด

def tax_agent_node(state: AgentState):
    """ผู้เชี่ยวชาญด้านภาษี"""
    print("[System: 💰 Tax Agent is processing...]")
    profile = state.get("user_profile", {})
    
    system_msg = SystemMessage(content=f"""
    คุณคือผู้เชี่ยวชาญด้านภาษี ข้อมูลผู้ใช้ตอนนี้คือ: {profile}
    ตอบสั้นๆ กระชับ เป็นภาษาไทย
    """)
    
    # 1. ป้องกัน Error จาก List: เราจะหาข้อความล่าสุดที่เป็นของ Human (User) เท่านั้น
    last_human_msg_content = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_human_msg_content = msg.content
            break
            
    # ถ้า content ดันเป็น list ให้ดึงมาแค่ text
    if isinstance(last_human_msg_content, list):
        last_human_msg_content = " ".join([i.get("text", "") for i in last_human_msg_content if isinstance(i, dict)])
        
    last_msg_str = str(last_human_msg_content).lower()
    
    # 2. ดึงตัวเลขจากข้อความแบบอัตโนมัติ (รองรับ 2,000,000 หรือเลขใดๆ)
    numbers = re.findall(r'\d+[\d,]*', last_msg_str)
    if numbers:
        # เอาเลขชุดแรกมาลบลูกน้ำออก แล้วแปลงเป็นตัวเลข
        income_val = float(numbers[0].replace(',', ''))
        profile["income"] = income_val
        profile["tax"] = calculate_tax_logic(income_val)
        profile["net_income"] = profile["income"] - profile["tax"]
    
    response = llm.invoke([system_msg] + state["messages"])
    
    # 3. ส่งงานกลับแบบ Silent Helper (ไม่เอา messages ยัดใส่ state)
    if state.get("route_to") == "need_tax_data":
        print("[System: 🤝 Tax Agent finished calculation. Returning data to Retirement Agent]")
        return {"user_profile": profile, "route_to": "retirement"}
        
    # ถ้า User ถามภาษีตรงๆ ค่อยให้ AI ตอบปกติ
    return {"messages": [response], "user_profile": profile, "route_to": "end"}

def retirement_agent_node(state: AgentState):
    """ผู้เชี่ยวชาญด้านเกษียณ"""
    print("[System: 👴 Retirement Agent is processing...]")
    profile = state.get("user_profile", {})
    
    # 1. ดึงข้อความล่าสุดของผู้ใช้
    last_human_msg_content = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_human_msg_content = msg.content
            break
            
    if isinstance(last_human_msg_content, list):
        last_human_msg_content = " ".join([i.get("text", "") for i in last_human_msg_content if isinstance(i, dict)])
        
    last_msg_str = str(last_human_msg_content).lower()
    
    # ========================================================
    # 🚨 [จุดที่เพิ่มใหม่] State Invalidation: ตรวจจับการเปลี่ยนตัวเลขรายได้
    # ========================================================
    numbers = re.findall(r'\d+[\d,]*', last_msg_str)
    if numbers:
        # ดึงตัวเลขแรกที่เจอในประโยค (ตั้งสมมติฐานว่าเป็นรายได้)
        potential_income = float(numbers[0].replace(',', ''))
        
        # เช็ค 2 เงื่อนไข: 
        # 1. เป็นตัวเลขที่มากพอจะเป็นรายได้ (เช่น > 10,000) ป้องกันดึงเลขผิดเช่น อายุ
        # 2. ตัวเลขนี้ "ไม่ตรง" กับ income เดิมใน Memory
        if potential_income > 10000 and potential_income != profile.get("income", 0):
            print(f"[System: 💡 พบข้อมูลรายได้ใหม่ ({potential_income:,.2f} บาท) ส่งให้ Tax Agent คำนวณภาษีใหม่!]")
            return {"route_to": "need_tax_data"}
    # ========================================================

    # ถ้าข้อมูลรายได้หลังหักภาษียังไม่มี (กรณีเข้าแชทมาครั้งแรก)
    if "net_income" not in profile:
        print("[System: 🔄 Retirement Agent needs net income. Requesting collaboration from Tax Agent!]")
        return {"route_to": "need_tax_data"}
        
    system_msg = SystemMessage(content=f"""
    คุณคือผู้เชี่ยวชาญด้านเกษียณ ข้อมูลผู้ใช้ตอนนี้คือ: {profile}
    ให้คำแนะนำการเกษียณโดยอิงจาก "รายได้สุทธิ (net_income)" ที่อยู่ใน profile
    ตอบเป็นภาษาไทย ให้กำลังใจ และชัดเจน
    """)
    
    response = llm.invoke([system_msg] + state["messages"])
    return {"messages": [response], "route_to": "end"}


# ==========================================
# 5. Build the LangGraph
# ==========================================
workflow = StateGraph(AgentState)

workflow.add_node("guardrail", guardrail_node)
workflow.add_node("tax", tax_agent_node)
workflow.add_node("retirement", retirement_agent_node)
workflow.add_node("fallback", fallback_node)

workflow.set_entry_point("guardrail")

# เงื่อนไขจาก Guardrail
def guardrail_route(state: AgentState):
    route = state.get("route_to")
    if route == "tax": return "tax"
    if route == "retirement": return "retirement"
    return "fallback"

workflow.add_conditional_edges(
    "guardrail",
    guardrail_route,
    {"tax": "tax", "retirement": "retirement", "fallback": "fallback"}
)

# เงื่อนไขการส่งต่อระหว่าง Agent หรือจบการทำงาน
def collaboration_route(state: AgentState):
    route = state.get("route_to")
    if route == "need_tax_data": return "tax"
    if route == "retirement": return "retirement"
    return END # ถ้าเป็น "end" หรืออื่นๆ ให้จบการทำงาน

workflow.add_conditional_edges("retirement", collaboration_route, {"tax": "tax", END: END})
workflow.add_conditional_edges("tax", collaboration_route, {"retirement": "retirement", END: END})
workflow.add_edge("fallback", END)

app = workflow.compile()


# ==========================================
# 6. Command Line Interface (Terminal Loop)
# ==========================================
if __name__ == "__main__":
    print("=====================================================")
    print("🤖 Personal Finance Chat AI Initialized!")
    print("Type 'exit' or 'quit' to end the conversation.")
    print("=====================================================")
    
    # Initial State
    current_state = {"messages": [], "user_profile": {}, "route_to": ""}
    
    while True:
        try:
            user_input = input("\n👤 You: ")
        except (KeyboardInterrupt, EOFError):
            break
            
        if user_input.lower() in ['exit', 'quit']:
            break
            
        current_state["messages"].append(HumanMessage(content=user_input))
        
        # Invoke the LangGraph workflow
        result_state = app.invoke(current_state)
        
        # Get the latest AI response
        raw_response = result_state["messages"][-1].content
        
        # --- [ส่วนที่แก้ไข] ตรวจสอบและสกัดเฉพาะข้อความ ---
        ai_text = ""
        if isinstance(raw_response, str):
            # ถ้าเป็น Text ธรรมดาอยู่แล้ว ก็ใช้ได้เลย
            ai_text = raw_response
        elif isinstance(raw_response, list):
            # ถ้าเป็น List โครงสร้างซับซ้อน ให้ดึงเฉพาะที่มี 'text'
            for item in raw_response:
                if isinstance(item, dict) and 'text' in item:
                    ai_text += item['text']
        else:
            ai_text = str(raw_response) # ดักไว้เผื่อเป็น Format แปลกๆ
            
        print(f"\n🤖 AI:\n{ai_text}")
        
        # Update current state for the next turn
        current_state["messages"] = result_state["messages"]
        current_state["user_profile"] = result_state.get("user_profile", {})