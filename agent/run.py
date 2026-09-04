import sys, uuid
from langgraph.types import Command
from .graph import build
from . import tools

def main():
    instruction = sys.argv[1] if len(sys.argv) > 1 else \
        "Buy 2 boxes of kaju katli, keep it under Rs 2000"
    mandate_id  = sys.argv[2]
    cap_paise   = int(sys.argv[3]) if len(sys.argv) > 3 else 200000

    trace_id = f"agt_{uuid.uuid4().hex[:8]}"
    graph    = build()
    config   = {"configurable": {"thread_id": trace_id}}

    state = {"instruction": instruction, "trace_id": trace_id,
             "mandate_id": mandate_id, "cap_paise": cap_paise,
             "notes": [], "status": "running"}

    print(f"\ntrace: {trace_id}\n")
    result = graph.invoke(state, config)

    # the graph can pause more than once
    while "__interrupt__" in result:
        ask  = result["__interrupt__"][0].value
        kind = ask.get("kind")

        if kind == "budget_scope":
            print(f"\n  {ask['message']}")
            answer = input("  total / per item: ")
        elif kind == "over_budget":
            print(f"\n  {ask['message']}")
            answer = input("  Continue? (y/n): ")
        elif kind == "not_affordable":
            print(f"\n  {ask['message']}")
            answer = input("  Take it? (show / n): ")
        else:
            print(f"\n  {ask['summary']}")
            print(f"  Total: Rs {ask['total_paise']/100:.0f}")
            answer = input("  Approve? (y/n): ")

        result = graph.invoke(Command(resume=answer), config)

    print("\n--- agent trail ---")
    for n in result["notes"]:
        print(" ", n)

    print(f"\nstatus: {result['status']}")
    if result.get("refusal_reason"):
        print(f"reason: {result['refusal_reason']}")
    if result.get("payment_url"):
        print(f"pay at: {result['payment_url']}")
    print(f"\nmerchant trail: curl.exe http://127.0.0.1:8000/audit/{trace_id}")

if __name__ == "__main__":
    main()