#!/usr/bin/env python3
"""
virtual_waiter_prototype.py - Perpetual Bistro virtual waiter (AI Lab Activity 6.1)

Interactive CLI powered by Ollama (qwen2.5) + SQLite + the FSM in fsm_agent.py.

    ollama pull qwen2.5          # once
    ollama serve                 # if it is not already running
    python virtual_waiter_prototype.py

Useful options
    --reset-db     rebuild restaurant.db (restores stock levels)
    --model NAME   any Ollama model that follows JSON instructions (default qwen2.5)
    --log FILE     where the JSON conversation log is saved (default conversation_log.json)
    --demo         run a fully scripted, offline conversation (no Ollama needed) and
                   write sample_conversation_log.json. Handy for testing the FSM + tools.

I/O is abstracted behind get_input() / send_output() (Lesson 6.5.1) so a speech-to-text /
text-to-speech wrapper can replace them later without touching the agent.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import db_tools as db
from fsm_agent import FSMAgent, LLMError, OllamaClient

HERE = Path(__file__).resolve().parent


# ----------------------------------------------------------------------
# I/O abstraction layer - swap these two for STT / TTS later
# ----------------------------------------------------------------------
def get_input(prompt: str = "You: ") -> str:
    return input(prompt)


def send_output(text: str) -> None:
    print(f"\nWaiter: {text}\n")


# ----------------------------------------------------------------------
def run_session(agent: FSMAgent, read=get_input, write=send_output) -> None:
    write(agent.start())
    while True:
        try:
            text = read()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.strip().lower() in ("/quit", "/exit", "quit", "exit"):
            write("Goodbye! Thanks for visiting Perpetual Bistro.")
            break
        write(agent.handle(text))
    agent.shutdown()


def check_ollama(host: str, model: str) -> None:
    try:
        with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=5) as resp:
            names = [m["name"] for m in json.loads(resp.read()).get("models", [])]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        sys.exit(f"Could not reach Ollama at {host}.\n"
                 "Start it with `ollama serve`, or try `python virtual_waiter_prototype.py --demo` "
                 "to run the offline scripted demo.")
    if not any(n == model or n.startswith(model + ":") for n in names):
        sys.exit(f"Model '{model}' is not installed. Run:  ollama pull {model}")


# ----------------------------------------------------------------------
# Offline demo: a scripted stand-in for Qwen, used to test FSM + tools
# ----------------------------------------------------------------------
DEMO_INPUTS = [
    "Table 4",
    "What drinks do you have?",
    "I'd like 2 cheeseburgers without onions, extra cheese",
    "Add an order of onion rings",
    "One French fries instead, and an iced tea",
    "Does the cheeseburger have dairy?",
    "Actually remove the garden salad",
    "What's the capital of France?",
    "That's all",
    "Yes",
    "GCash",          # the waiter now asks how the customer will pay before confirming
]


def _call(tool: str, **arguments) -> str:
    return json.dumps({"tool": tool, "arguments": arguments})


DEMO_LLM_OUTPUTS = [
    # turn 2: menu question
    _call("get_menu_by_category", category="Drinks"),
    "We have Iced Tea (PHP 50), Mango Smoothie (PHP 90), Cola (PHP 45) and Bottled Water (PHP 30).",
    # turn 3: add with modifications
    _call("add_item_to_order", item_name="Cheeseburger", qty=2, instructions="No onions, extra cheese"),
    "Added 2 Cheeseburgers (no onions, extra cheese) to your tab. Anything else?",
    # turn 4: out of stock
    _call("add_item_to_order", item_name="Onion Rings", qty=1, instructions=""),
    "Sorry, the Onion Rings are sold out today. I can offer French Fries (PHP 80) or a Garden Salad (PHP 95) instead.",
    # turn 5: two tool calls in a single message
    _call("add_item_to_order", item_name="French Fries", qty=1) + "\n" +
    _call("add_item_to_order", item_name="Iced Tea", qty=1),
    "Done - 1 French Fries and 1 Iced Tea are on your tab.",
    # turn 6: allergen question
    _call("check_item_allergen", item_id=1, allergen="dairy"),
    "Yes, the Cheeseburger contains dairy (it also has gluten and egg).",
    # turn 7: removing something that is not on the order
    _call("remove_item_from_order", item_name="Garden Salad"),
    "The Garden Salad isn't on your order - you currently have Cheeseburgers, French Fries and an Iced Tea.",
    # turn 8: out of domain
    "I'm only able to help with Perpetual Bistro orders. Can I get you anything else from the menu?",
]


class ScriptedLLM:
    """Replays canned model outputs in order (offline testing only)."""

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)

    def __call__(self, messages: list[dict]) -> str:
        if not self.outputs:
            raise LLMError("scripted LLM ran out of outputs")
        return self.outputs.pop(0)


def run_demo(log_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db.set_db_path(Path(tmp) / "demo.db")
        db.init_db(reset=True)
        agent = FSMAgent(ScriptedLLM(DEMO_LLM_OUTPUTS), llm_label="scripted-stub (offline demo, not a real Qwen run)")
        print(f"Waiter: {agent.start()}\n")
        for line in DEMO_INPUTS:
            print(f"You: {line}")
            print(f"\nWaiter: {agent.handle(line)}\n")
        agent.save_log(str(log_path))
        print(f"[demo] log written to {log_path}")


# ----------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Perpetual Bistro virtual waiter (Ollama + SQLite + FSM)")
    ap.add_argument("--model", default="qwen2.5")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--db", default=str(HERE / "restaurant.db"))
    ap.add_argument("--log", default=str(HERE / "conversation_log.json"))
    ap.add_argument("--reset-db", action="store_true", help="rebuild the database (restores stock)")
    ap.add_argument("--demo", action="store_true", help="offline scripted demo; writes sample_conversation_log.json")
    args = ap.parse_args()

    if args.demo:
        run_demo(HERE / "sample_conversation_log.json")
        return

    db.set_db_path(args.db)
    db.init_db(reset=args.reset_db)
    check_ollama(args.host, args.model)

    agent = FSMAgent(OllamaClient(args.model, args.host), llm_label=args.model)
    print("Perpetual Bistro Virtual Waiter  (type /help for commands, /quit to leave)")
    try:
        run_session(agent)
    finally:
        agent.save_log(args.log)
        print(f"Conversation log saved to {args.log}")


if __name__ == "__main__":
    main()