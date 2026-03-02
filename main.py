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
# 🚨 HACK: Force disable SSL verification (Bypass) for Workshop
# ==========================================
original_client_init = httpx.Client.__init__

def unverified_client_init(self, *args, **kwargs):
    kwargs['verify'] = False # Force disable Certificate verification
    original_client_init(self, *args, **kwargs)

httpx.Client.__init__ = unverified_client_init

# Suppress warnings about insecure connections
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
    """Calculate personal income tax based on Thailand's progressive tax rates"""
    
    # Print Debug Message to show that the Tool was called
    print(f"\n[Debug: 🧮 Tool 'calculate_tax_logic' called | Initial income: {income:,.2f} THB]")
    
    # 1. Deduct maximum standard deduction (100,000) and personal allowance (60,000)
    # to convert annual income into "net income"
    net_income = max(0, income - 160000)
    print(f"[Debug: 🧮 Net income after basic deductions: {net_income:,.2f} THB]")
    
    tax = 0.0
    
    # 2. Calculate tax according to Thai progressive rates (top to bottom)
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
        # Portion below 150,000 is tax-exempt (0%)

    print(f"[Debug: 🧮 Calculation complete | Tax payable: {tax:,.2f} THB]\n")
    return tax

# ==========================================
# 4. Create Nodes (Agent Functions)
# ==========================================
def guardrail_node(state: AgentState):
    """Check scope boundaries"""
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
    """Handle out-of-scope requests"""
    print("[System: 🛑 Routing to Fallback Node]")
    msg = AIMessage(content="ขออภัยครับ ขอบเขตของผมดูแลเฉพาะเรื่อง **การคำนวณภาษี** และ **การวางแผนเกษียณ** เท่านั้นครับ")
    return {"messages": [msg], "route_to": "end"} # Clear routing variable

def tax_agent_node(state: AgentState):
    """Tax Expert Node"""
    print("[System: 💰 Tax Agent is processing...]")
    profile = state.get("user_profile", {})
    
    system_msg = SystemMessage(content=f"""
    คุณคือผู้เชี่ยวชาญด้านภาษี ข้อมูลผู้ใช้ตอนนี้คือ: {profile}
    ตอบสั้นๆ กระชับ เป็นภาษาไทย
    """)
    
    # 1. Prevent List errors: We will only find the latest Human (User) message
    last_human_msg_content = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_human_msg_content = msg.content
            break
            
    # If content happens to be a list, extract only the text
    if isinstance(last_human_msg_content, list):
        last_human_msg_content = " ".join([i.get("text", "") for i in last_human_msg_content if isinstance(i, dict)])
        
    last_msg_str = str(last_human_msg_content).lower()
    
    # 2. Extract numbers from text automatically (supports 2,000,000 or any number)
    numbers = re.findall(r'\d+[\d,]*', last_msg_str)
    if numbers:
        # Take the first set of numbers, remove commas, and convert to float
        income_val = float(numbers[0].replace(',', ''))
        profile["income"] = income_val
        profile["tax"] = calculate_tax_logic(income_val)
        profile["net_income"] = profile["income"] - profile["tax"]
    
    response = llm.invoke([system_msg] + state["messages"])
    
    # 3. Send back as a Silent Helper (do not append messages to state)
    if state.get("route_to") == "need_tax_data":
        print("[System: 🤝 Tax Agent finished calculation. Returning data to Retirement Agent]")
        return {"user_profile": profile, "route_to": "retirement"}
        
    # If User asks about tax directly, let the AI answer normally
    return {"messages": [response], "user_profile": profile, "route_to": "end"}

def retirement_agent_node(state: AgentState):
    """Retirement Expert Node"""
    print("[System: 👴 Retirement Agent is processing...]")
    profile = state.get("user_profile", {})
    
    # 1. Retrieve the user's latest message
    last_human_msg_content = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_human_msg_content = msg.content
            break
            
    if isinstance(last_human_msg_content, list):
        last_human_msg_content = " ".join([i.get("text", "") for i in last_human_msg_content if isinstance(i, dict)])
        
    last_msg_str = str(last_human_msg_content).lower()
    
    # ========================================================
    # 🚨 [New Addition] State Invalidation: Detect changes in income numbers
    # ========================================================
    numbers = re.findall(r'\d+[\d,]*', last_msg_str)
    if numbers:
        # Extract the first number found in the sentence (assume it's the income)
        potential_income = float(numbers[0].replace(',', ''))
        
        # Check 2 conditions: 
        # 1. The number is large enough to be an income (e.g., > 10,000) to prevent mistakenly extracting age
        # 2. This number does "not match" the existing income in Memory
        if potential_income > 10000 and potential_income != profile.get("income", 0):
            print(f"[System: 💡 Found new income data ({potential_income:,.2f} THB). Routing to Tax Agent for recalculation!]")
            return {"route_to": "need_tax_data"}
    # ========================================================

    # If net income data is not yet available (e.g., first time entering the chat)
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

# Conditions from Guardrail
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

# Conditions for routing between Agents or ending the workflow
def collaboration_route(state: AgentState):
    route = state.get("route_to")
    if route == "need_tax_data": return "tax"
    if route == "retirement": return "retirement"
    return END # If it's "end" or others, terminate the workflow

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
        
        # --- [Modified Section] Check and extract only text ---
        ai_text = ""
        if isinstance(raw_response, str):
            # If it's already plain text, use it directly
            ai_text = raw_response
        elif isinstance(raw_response, list):
            # If it's a complex List structure, extract only the parts containing 'text'
            for item in raw_response:
                if isinstance(item, dict) and 'text' in item:
                    ai_text += item['text']
        else:
            ai_text = str(raw_response) # Catch-all for unusual formats
            
        print(f"\n🤖 AI:\n{ai_text}")
        
        # Update current state for the next turn
        current_state["messages"] = result_state["messages"]
        current_state["user_profile"] = result_state.get("user_profile", {})