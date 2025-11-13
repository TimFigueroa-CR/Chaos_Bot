
"""
Chaos Bot: An unpredictable, erratic, manic bot that sets many edge cases to test other bots.

Also, because it's funny.


Usage:
    pip install websockets==12.0
    python chaos_bot.py --team CHAOS_BOT --url wss://poker-bot-arena.fly.dev/

    # Local A/B testing against the practice server
    python sample_bot.py --team TEAM_NAME --bot A --url wss://poker-bot-arena.fly.dev/



This bot randomly switches styles from the following list:
** Maniac: Does not fold, just raises. It's funny.
** Fancy: Only plays with very strong/premium hands. Oooh fancy. Look at Mr. Fancypants over here.
** Calling Station: Mostly calls, never raises the bet; which is also funny.
** Opposite day: For weak hands, it plays them strongly. For strong hands, it plays them weakly. 
** Superstitious: Makes decisions based on card suits, because... you guessed it, it's pretty funny.
** Selective Aggressive: Tries to play kinda normally(boring as heck). Aggresive with good hands, non-agressive with bad hands.

Also, the bot has chaotic/random behaviour that can pop up, such as a 2% chance of going all in. Because it is funny.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import websockets

LOGGER = logging.getLogger("chaos_bot")
STREAM_HANDLER = logging.StreamHandler()
STREAM_HANDLER.setFormatter(logging.Formatter("%(message)s"))
if not LOGGER.handlers:
    LOGGER.addHandler(STREAM_HANDLER)
LOGGER.propagate = False

USE_UNICODE_CARDS = True
SUIT_SYMBOLS = {"c": "♣", "d": "♦", "h": "♥", "s": "♠"}


@dataclass
class ActionContext:
    # Hand identification
    hand_id: str
    seat: int
    phase: str
    
    # Your cards and stack
    hole_cards: list[str]  # Your two hole cards (e.g., ["Ah", "Kd"])
    stack: int
    committed: int
    to_call: int  # Amount needed to call
    
    # Table state
    pot: int  # Current pot size
    current_bet: int  # Current highest bet on table
    community: list[str]  # Community cards on board (flop, turn, river)
    
    # Table configuration
    button: int  # Seat number of the button
    sb: int  # Small blind amount
    bb: int  # Big blind amount
    seats: int  # Total number of seats
    
    # Opponent information
    players: list[dict[str, Any]]  # List of all players with seat, stack, has_folded, committed
    
    # Action constraints
    legal: list[str]  # Available actions (e.g., ["CHECK", "CALL"], ["FOLD", "CALL", "RAISE_TO"])
    call_amount: Optional[int]  # Amount needed to call
    min_raise_to: Optional[int]  # Minimum total to raise to
    max_raise_to: Optional[int]  # Maximum total to raise to (usually stack + committed)
    min_raise_increment: int  # Minimum raise increment
    
    # Time remaining
    time_ms: int  # Time remaining to make decision


class ChaosBot:
    def __init__(self):
        self.mode = None
        self.hand_count = 0
        self.mode_duration = random.randint(2, 5) 
        self.last_action = None
        
    def select_mode(self):
        """Randomly select a playing mode for the next few hands(every 2 to 5 hands)"""
        modes = ["maniac", "fancy", "calling_station", "opposite_day", "superstitious", "selective_aggressive"]
        self.mode = random.choice(modes)
        self.mode_duration = random.randint(2, 5)
        LOGGER.info(f"[CHAOS] Switching to {self.mode.upper()} mode for {self.mode_duration} hands")
        return self.mode
    
    def hand_strength(self, hole_cards: list[str]) -> float:
        """Crude hand strength estimation"""
        ranks = {'2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, 
                '9': 9, 'T': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}
        
        if not hole_cards or len(hole_cards) < 2:
            return 0.0
            
        card1_rank = ranks.get(hole_cards[0][0], 2)
        card2_rank = ranks.get(hole_cards[1][0], 2)
        
        # Check for pairs
        is_pair = hole_cards[0][0] == hole_cards[1][0]
        
        # Simple strength calculation
        strength = max(card1_rank, card2_rank)
        if is_pair:
            strength *= 2
        if card1_rank > 10 and card2_rank > 10:  # Both high cards
            strength *= 1.5
            
        return strength / 28.0  # Normalize to 0-1
    
    def is_premium_hand(self, hole_cards: list[str]) -> bool:
        """Check if hand is in top 10%"""
        premium_hands = {'AA', 'KK', 'QQ', 'JJ', 'AK', 'AQ'}
        if len(hole_cards) < 2:
            return False
        hand_str = ''.join(sorted([card[0] for card in hole_cards], reverse=True))
        return hand_str in premium_hands
    
    def is_weak_hand(self, hole_cards: list[str]) -> bool:
        """Check if hand is in bottom 20%"""
        weak_hands = {'72', '73', '74', '75', '76', '82', '83', '84', '85', 
                     '92', '93', '94', '32', '42', '52', '62'}
        if len(hole_cards) < 2:
            return False
        hand_str = ''.join(sorted([card[0] for card in hole_cards], reverse=True))
        return hand_str in weak_hands
    
    def has_heart(self, hole_cards: list[str]) -> bool:
        """Check if any card is a heart"""
        return any('h' in card for card in hole_cards)
    
    def has_two_clubs(self, hole_cards: list[str]) -> bool:
        """Check if both cards are clubs"""
        return len(hole_cards) == 2 and all('c' in card for card in hole_cards)
    
    def should_fold_to_large_bet(self, ctx: ActionContext) -> bool:
        """Avoid calling massive bets without good hands"""
        if ctx.call_amount is None or ctx.pot == 0:
            return False
            
        hand_strength = self.hand_strength(ctx.hole_cards)
        bet_to_pot_ratio = ctx.call_amount / ctx.pot if ctx.pot > 0 else 0
        stack_threat = ctx.call_amount / ctx.stack if ctx.stack > 0 else 1
        
        # Fold to very large bets (>50% of pot) with weak hands
        if bet_to_pot_ratio > 0.5 and stack_threat > 0.1:
            if hand_strength < 0.4:  # Weak hand
                return random.random() < 0.7  # 70% chance to fold
                
        # Fold if bet would put us at risk of elimination
        if ctx.stack - ctx.call_amount < ctx.bb * 3 and hand_strength < 0.6:
            return random.random() < 0.8
            
        return False
    
    def sensible_raise_size(self, ctx: ActionContext) -> int:
        """More reasonable raise sizing"""
        min_raise = ctx.min_raise_to
        max_raise = ctx.max_raise_to or (ctx.stack + ctx.committed)
        
        # Don't risk too much with weak hands
        hand_strength = self.hand_strength(ctx.hole_cards)
        
        if hand_strength > 0.7:  # Strong hand
            # Raise more with good hands
            if random.random() < 0.3:  # 30% chance for big raise
                return min(max_raise, min_raise * 3)
            else:
                return random.randint(min_raise, min(min_raise * 2, max_raise))
        else:
            # Smaller raises with mediocre hands
            return random.randint(min_raise, min(min_raise + ctx.min_raise_increment * 2, max_raise))
    
    def maniac_mode(self, ctx: ActionContext) -> Tuple[str, Optional[int]]:
        """Aggressive but not suicidal"""
        # Occasionally fold to massive bets even in maniac mode
        if self.should_fold_to_large_bet(ctx):
            LOGGER.info("[MANIAC] Folding to large bet for survival")
            return "FOLD", None
            
        if "RAISE_TO" in ctx.legal and ctx.min_raise_to:
            raise_amount = self.sensible_raise_size(ctx)
            return "RAISE_TO", raise_amount
        elif "CALL" in ctx.legal:
            # Avoid calling huge bets with trash
            if ctx.call_amount and ctx.call_amount > ctx.pot and self.hand_strength(ctx.hole_cards) < 0.3:
                if random.random() < 0.6:  # 60% chance to fold
                    return "FOLD", None
            return "CALL", None
        elif "CHECK" in ctx.legal:
            return "CHECK", None
        else:
            return "FOLD", None
    
    def fancy_mode(self, ctx: ActionContext) -> Tuple[str, Optional[int]]:
        """Only plays amazing hands, fold everything else that isn't premium"""
        if self.is_premium_hand(ctx.hole_cards):
            if "RAISE_TO" in ctx.legal and ctx.min_raise_to:
                return "RAISE_TO", ctx.min_raise_to
            elif "CALL" in ctx.legal:
                return "CALL", None
            elif "CHECK" in ctx.legal:
                return "CHECK", None
        return "FOLD", None
    
    def calling_station_mode(self, ctx: ActionContext) -> Tuple[str, Optional[int]]:
        """Mostly, just calls"""
        # Folds to massive overbets, just so it doesn't die that quickly
        if ctx.call_amount and ctx.call_amount > ctx.pot * 2:
            hand_str = self.hand_strength(ctx.hole_cards)
            if hand_str < 0.5:  # Weak hand facing huge bet
                LOGGER.info("[CALLING_STATION] Folding to massive overbet")
                return "FOLD", None
        
        if "CALL" in ctx.legal:
            if ctx.call_amount and ctx.call_amount > ctx.bb * 10:
                hand_str = self.hand_strength(ctx.hole_cards)
                if hand_str < 0.4 and random.random() < 0.7:
                    return "FOLD", None
            return "CALL", None
        elif "CHECK" in ctx.legal:
            return "CHECK", None
        else:
            return "FOLD", None
    
    def opposite_day_mode(self, ctx: ActionContext) -> Tuple[str, Optional[int]]:
        """Play weak hands strong, strong hands weak"""
        is_strong = self.is_premium_hand(ctx.hole_cards)
        is_weak = self.is_weak_hand(ctx.hole_cards)
        
        # 80% chance to play backwards
        if random.random() < 0.8:
            if is_strong:
                # Play strong hands weak
                if "CHECK" in ctx.legal:
                    return "CHECK", None
                elif "CALL" in ctx.legal:
                    return "CALL", None
            elif is_weak:
                # Play weak hands strong
                if "RAISE_TO" in ctx.legal and ctx.min_raise_to:
                    raise_amount = self.sensible_raise_size(ctx)
                    return "RAISE_TO", raise_amount
        
        # Default random behavior
        return self.random_action(ctx)
    
    def superstitious_mode(self, ctx: ActionContext) -> Tuple[str, Optional[int]]:
        """Make decisions based on card suits"""
        if self.has_two_clubs(ctx.hole_cards):
            LOGGER.info("[SUPERSTITION] Two clubs! Must fold!")
            return "FOLD", None
        elif self.has_heart(ctx.hole_cards):
            LOGGER.info("[SUPERSTITION] Heart detected! Must raise!")
            if "RAISE_TO" in ctx.legal and ctx.min_raise_to:
                raise_amount = self.sensible_raise_size(ctx)
                return "RAISE_TO", raise_amount
        
        # Default to random for other cases
        return self.random_action(ctx)
    
    def selective_aggressive_mode(self, ctx: ActionContext) -> Tuple[str, Optional[int]]:
        """Aggressively plays with good hands, proceeds cautiously with bad hands"""
        hand_str = self.hand_strength(ctx.hole_cards)
        
        if hand_str > 0.6:  # Strong hand
            if "RAISE_TO" in ctx.legal and ctx.min_raise_to:
                raise_amount = self.sensible_raise_size(ctx)
                return "RAISE_TO", raise_amount
            elif "CALL" in ctx.legal:
                return "CALL", None
        elif hand_str < 0.3:  # Weak hand
            if "CHECK" in ctx.legal:
                return "CHECK", None
            elif ctx.call_amount and ctx.call_amount > ctx.bb * 5:
                return "FOLD", None
            elif "CALL" in ctx.legal:
                return "CALL", None
        
        # Medium strength - mixed strategy
        return self.random_action(ctx)
    
    def random_action(self, ctx: ActionContext) -> Tuple[str, Optional[int]]:
        """Less self-destructive random actions"""
        roll = random.randint(1, 100)
        
        # 2% chance to go all-in
        if roll <= 2 and ctx.stack > ctx.bb * 10:  # Only all-in with reasonable stack
            LOGGER.info("[CHAOS] RANDOM ALL-IN!")
            if "RAISE_TO" in ctx.legal:
                return "RAISE_TO", ctx.stack + ctx.committed
            elif "CALL" in ctx.legal:
                return "CALL", None
        
        # 8% chance to fold randomly
        elif roll <= 10:
            # Don't fold as often with good hands
            if self.hand_strength(ctx.hole_cards) > 0.6 and random.random() < 0.7:
                if "CHECK" in ctx.legal:
                    return "CHECK", None
                elif "CALL" in ctx.legal:
                    return "CALL", None
            LOGGER.info("[CHAOS] Random fold!")
            return "FOLD", None
        
        # 20% chance to raise randomly
        elif roll <= 30 and "RAISE_TO" in ctx.legal and ctx.min_raise_to:
            LOGGER.info("[CHAOS] Random raise!")
            raise_amount = self.sensible_raise_size(ctx)
            return "RAISE_TO", raise_amount
        
        # 30% chance to check if available
        elif roll <= 60 and "CHECK" in ctx.legal:
            return "CHECK", None
        
        # Otherwise call while being careful with large bets
        elif "CALL" in ctx.legal:
            if ctx.call_amount and ctx.call_amount > ctx.pot and self.hand_strength(ctx.hole_cards) < 0.3:
                if random.random() < 0.4:  # 40% chance to fold instead
                    return "FOLD", None
            return "CALL", None
        
        # Fallback
        return "FOLD", None
    
    def choose_action(self, ctx: ActionContext) -> Tuple[str, Optional[int]]:
        """Main decision function for chaos bot"""
        self.hand_count += 1
        
        # Change mode if duration expired
        if self.mode is None or self.hand_count % self.mode_duration == 0:
            self.select_mode()
        
        LOGGER.info(f"[CHAOS] Mode: {self.mode.upper()}, Hand: {self.hand_count}, Cards: {ctx.hole_cards}")
        
        # Special short-stack logic
        if ctx.stack < ctx.bb * 5:  # Very short stacked
            if self.is_premium_hand(ctx.hole_cards):
                if "RAISE_TO" in ctx.legal:
                    return "RAISE_TO", ctx.stack + ctx.committed
                elif "CALL" in ctx.legal:
                    return "CALL", None
            elif "FOLD" in ctx.legal:
                return "FOLD", None
        
        # Dispatch to appropriate mode handler
        if self.mode == "maniac":
            return self.maniac_mode(ctx)
        elif self.mode == "fancy":
            return self.fancy_mode(ctx)
        elif self.mode == "calling_station":
            return self.calling_station_mode(ctx)
        elif self.mode == "opposite_day":
            return self.opposite_day_mode(ctx)
        elif self.mode == "superstitious":
            return self.superstitious_mode(ctx)
        elif self.mode == "selective_aggressive":
            return self.selective_aggressive_mode(ctx)
        else:
            return self.random_action(ctx)

# Global Chaos Bot instance
chaos_bot = ChaosBot()

def choose_action(ctx: ActionContext) -> Tuple[str, Optional[int]]:
    return chaos_bot.choose_action(ctx)


def fallback_action(ctx: ActionContext) -> Tuple[str, Optional[int]]:
    """Select the safest legal move (check > call > fold > first legal)."""
    if "CHECK" in ctx.legal:
        return "CHECK", None
    if "CALL" in ctx.legal:
        return "CALL", None
    if "FOLD" in ctx.legal:
        return "FOLD", None
    if ctx.legal:
        return ctx.legal[0], None
    LOGGER.error("No legal actions supplied; defaulting to FOLD")
    return "FOLD", None


def sanitize_action(action: str, amount: Optional[int], ctx: ActionContext) -> Tuple[str, Optional[int]]:
    """Ensure the outgoing action abides by the host constraints."""
    if action not in ctx.legal:
        LOGGER.warning("Illegal action '%s' requested; falling back", action)
        return fallback_action(ctx)

    if action == "RAISE_TO":
        if ctx.min_raise_to is None:
            LOGGER.warning("RAISE_TO chosen but min_raise_to missing; falling back")
            return fallback_action(ctx)
        if amount is None:
            amount = ctx.min_raise_to
        min_allowed = ctx.min_raise_to
        max_allowed = ctx.max_raise_to
        bankroll_cap = ctx.stack + ctx.committed
        if max_allowed is None or max_allowed > bankroll_cap:
            max_allowed = bankroll_cap
        if amount < min_allowed:
            LOGGER.warning("Raise total %s below minimum %s; clamping", amount, min_allowed)
            amount = min_allowed
        if amount > max_allowed:
            LOGGER.warning("Raise total %s above maximum %s; clamping", amount, max_allowed)
            amount = max_allowed
        if amount < min_allowed:
            LOGGER.warning("Unable to find legal raise amount; falling back")
            return fallback_action(ctx)
        return action, int(amount)

    if action == "CALL" and "CALL" not in ctx.legal:
        LOGGER.warning("CALL chosen but not legal; falling back")
        return fallback_action(ctx)

    if action == "CHECK" and "CHECK" not in ctx.legal:
        LOGGER.warning("CHECK chosen but not legal; falling back")
        return fallback_action(ctx)

    return action, amount


async def play_hand(
    websocket: websockets.WebSocketServerProtocol,
    team_name: str,
    bot_label: Optional[str] = None,
) -> None:
    """Listen for host messages, respond to act prompts, and log hand summaries."""

    display_name = f"{team_name} ({bot_label})" if bot_label else team_name

    state: Dict[str, Any] = {
        "seat": None,
        "seat_count": None,
        "hand_id": None,
        "phase": "PRE_FLOP",
        "phase_label": "PRE",
        "hand_log": None,
        "hand_counter": 0,
        "team_name": team_name,
        "team_display": display_name,
        "bot_label": bot_label,
        "seat_map": {},
    }

    def set_phase(raw_phase: Optional[str]) -> None:
        if not raw_phase:
            return
        state["phase"] = raw_phase
        state["phase_label"] = {
            "PRE_FLOP": "PRE",
            "FLOP": "FLOP",
            "TURN": "TURN",
            "RIVER": "RIVER",
            "SHOWDOWN": "SHOW",
        }.get(raw_phase, raw_phase)

    def seat_label(seat: Optional[int]) -> str:
        if seat is None:
            return "Seat ?"
        if seat == state.get("seat"):
            name = state.get("team_display") or state.get("team_name") or f"Seat {seat}"
            return name
        seat_count = state.get("seat_count")
        team = state.get("seat_map", {}).get(seat)
        if team:
            if seat_count == 2:
                return f"{team} (opponent, seat {seat})"
            return f"{team} (seat {seat})"
        if seat_count == 2:
            return f"Opponent (seat {seat})"
        return f"Seat {seat}"

    def format_stacks(stacks: list[dict[str, Any]]) -> str:
        if not stacks:
            return "-"
        return ", ".join(
            f"{seat_label(entry.get('seat'))}:{entry.get('stack')}"
            for entry in stacks
        )

    async for raw in websocket:
        message = json.loads(raw)
        msg_type = message.get("type")

        if msg_type == "welcome":
            state["seat"] = message.get("seat")
            cfg = message.get("config", {})
            state["seat_count"] = cfg.get("seats")
            register_seat(state, state["seat"], state.get("team_display"))
            LOGGER.info(
                "[welcome] seat %s | variant=%s seats=%s sb=%s bb=%s",
                seat_label(state["seat"]),
                cfg.get("variant"),
                cfg.get("seats"),
                cfg.get("sb"),
                cfg.get("bb"),
            )
            continue

        if msg_type == "ab_status":
            LOGGER.info(
                "[practice] waiting for partner | bot=%s state=%s",
                message.get("bot"),
                message.get("state"),
            )
            continue

        if msg_type == "start_hand":
            # Reset per-hand state and record baseline info for the recap.
            state["hand_id"] = message.get("hand_id")
            set_phase("PRE_FLOP")
            log = {
                "hand_id": state["hand_id"],
                "button": message.get("button"),
                "start_stacks": message.get("stacks", []),
                "actions": {"PRE": [], "FLOP": [], "TURN": [], "RIVER": []},
                "board": [],
                "board_by_phase": {},
                "showdown": [],
                "payouts": [],
                "eliminations": [],
            }
            state["hand_log"] = log
            LOGGER.info(
                "[hand %s] start | button %s | stacks %s",
                state["hand_id"],
                seat_label(log["button"]),
                format_stacks(log["start_stacks"]),
            )
            continue

        if msg_type == "lobby":
            # Keep track of player names when the host sends lobby updates.
            players = message.get("players", [])
            for player in players:
                register_seat(state, player.get("seat"), player.get("team"))
            continue

        if msg_type == "event":
            # Record table events to replay later in the summary.
            log = state.get("hand_log")
            if log is None:
                continue
            ev = message.get("ev")
            if ev == "POST_BLINDS":
                log["actions"]["PRE"].append(f"{seat_label(message.get('sb_seat'))} posts SB {message.get('sb')}")
                log["actions"]["PRE"].append(f"{seat_label(message.get('bb_seat'))} posts BB {message.get('bb')}")
            elif ev in {"BET", "CALL", "CHECK", "FOLD"}:
                verbs = {
                    "BET": "bets",
                    "CALL": "calls",
                    "CHECK": "checks",
                    "FOLD": "folds",
                }
                amount = message.get("amount")
                amount_str = f" {amount}" if amount is not None else ""
                phase_key = state.get("phase_label", "PRE")
                log["actions"].setdefault(phase_key, [])
                log["actions"][phase_key].append(f"{seat_label(message.get('seat'))} {verbs[ev]}{amount_str}")
            elif ev == "FLOP":
                set_phase("FLOP")
                log["board"] = list(message.get("cards", []))
                log["board_by_phase"]["FLOP"] = list(log["board"])
            elif ev == "TURN":
                card = message.get("card")
                if card:
                    log["board"].append(card)
                set_phase("TURN")
                log["board_by_phase"]["TURN"] = list(log["board"])
            elif ev == "RIVER":
                card = message.get("card")
                if card:
                    log["board"].append(card)
                set_phase("RIVER")
                log["board_by_phase"]["RIVER"] = list(log["board"])
            elif ev == "SHOWDOWN":
                set_phase("SHOWDOWN")
                log["showdown"].append(
                    {
                        "seat": message.get("seat"),
                        "hand": list(message.get("hand", [])),
                        "rank": message.get("rank"),
                    }
                )
            elif ev == "POT_AWARD":
                log["payouts"].append(
                    {
                        "seat": message.get("seat"),
                        "amount": message.get("amount"),
                    }
                )
            elif ev == "ELIMINATED":
                seat = message.get("seat")
                if seat is not None:
                    log["eliminations"].append(seat)
            else:
                LOGGER.debug("Unhandled event %s for hand %s", ev, state.get("hand_id"))
            continue

        if msg_type == "act":
            # Host is asking for action; choose a move and respond.
            set_phase(message.get("phase"))
            you = message.get("you", {})
            table = message.get("table", {})
            ctx = ActionContext(
                # Hand identification
                hand_id=message["hand_id"],
                seat=message["seat"],
                phase=message.get("phase", "PRE_FLOP"),
                # Your cards and stack
                hole_cards=list(you.get("hole", [])),
                stack=you.get("stack", 0),
                committed=you.get("committed", 0),
                to_call=you.get("to_call", 0),
                # Table state
                pot=message.get("pot", 0),
                current_bet=message.get("current_bet", 0),
                community=list(message.get("community", [])),
                # Table configuration
                button=table.get("button", 0),
                sb=table.get("sb", 0),
                bb=table.get("bb", 0),
                seats=table.get("seats", 0),
                # Opponent information
                players=list(message.get("players", [])),
                # Action constraints
                legal=list(message.get("legal", [])),
                call_amount=message.get("call_amount"),
                min_raise_to=message.get("min_raise_to"),
                max_raise_to=message.get("max_raise_to"),
                min_raise_increment=message.get("min_raise_increment", 0),
                # Time remaining
                time_ms=you.get("time_ms", 0),
            )
            action, amount = choose_action(ctx)
            action, amount = sanitize_action(action, amount, ctx)
            payload: Dict[str, Any] = {
                "type": "action",
                "v": 1,
                "hand_id": ctx.hand_id,
                "action": action,
            }
            if amount is not None:
                payload["amount"] = int(amount)
            LOGGER.debug("Sending action: %s", payload)
            await websocket.send(json.dumps(payload))
            continue

        if msg_type == "end_hand":
            # Emit a human-readable recap now that the hand is complete.
            log = state.get("hand_log")
            final_stacks = format_stacks(message.get("stacks", []))
            hand_id = message.get("hand_id") or state.get("hand_id")
            if log:
                LOGGER.info(
                    "[hand %s] summary | button %s | start stacks %s",
                    log["hand_id"],
                    seat_label(log["button"]),
                    format_stacks(log["start_stacks"]),
                )
                phase_order = [
                    ("PRE", "Preflop"),
                    ("FLOP", "Flop"),
                    ("TURN", "Turn"),
                    ("RIVER", "River"),
                ]
                actions = log.get("actions", {})
                board_by_phase = log.get("board_by_phase", {})
                for key, label in phase_order:
                    entries = actions.get(key, [])
                    board_cards = None if key == "PRE" else board_by_phase.get(key)
                    if not entries and not board_cards:
                        continue
                    if key == "PRE":
                        LOGGER.info("  %s:", label)
                    else:
                        board_str = render_cards(board_cards) if board_cards else "--"
                        LOGGER.info("  %s [%s]:", label, board_str)
                    for entry in entries:
                        LOGGER.info("    %s", entry)
                if log["showdown"]:
                    LOGGER.info("  Showdown:")
                    for entry in log["showdown"]:
                        LOGGER.info(
                            "    %s shows %s (%s)",
                            seat_label(entry["seat"]),
                            render_cards(entry["hand"]),
                            entry.get("rank"),
                        )
                if log["payouts"]:
                    LOGGER.info("  Payouts:")
                    for entry in log["payouts"]:
                        LOGGER.info("    %s +%s", seat_label(entry["seat"]), entry["amount"])
                if log["eliminations"]:
                    eliminated = ", ".join(seat_label(seat) for seat in log["eliminations"])
                    LOGGER.info("  Eliminated: %s", eliminated)
                state["hand_counter"] = state.get("hand_counter", 0) + 1
            LOGGER.info("[hand %s] end | stacks %s", hand_id, final_stacks)
            LOGGER.info("")
            state["hand_log"] = None
            continue

        if msg_type == "match_end":
            final_stacks = message.get("final_stacks", [])
            for entry in final_stacks:
                register_seat(state, entry.get("seat"), entry.get("team"))
            winner = message.get("winner") or {}
            winner_label = (
                f"{seat_label(winner.get('seat'))}"
                if winner
                else "None"
            )
            LOGGER.info(
                "[match] winner=%s final_stacks=%s",
                winner_label,
                format_stacks(message.get("final_stacks", [])),
            )
            LOGGER.info("")
            break

        if msg_type == "error":
            LOGGER.warning("[error] %s", message)
            continue

        if msg_type == "snapshot":
            LOGGER.debug("[snapshot] %s", message)
            continue

        LOGGER.debug("Ignoring message type=%s", msg_type)


async def run_bot(team: str, url: str, bot: Optional[str] = None) -> None:
    try:
        async with websockets.connect(url) as ws:
            hello = {
                "type": "hello",
                "v": 1,
                "team": team,
            }
            if bot:
                hello["bot"] = bot
            await ws.send(json.dumps(hello))
            label = f"{team} ({bot})" if bot else team
            LOGGER.info("[connect] %s as %s", url, label)
            # Stay inside play_hand until the server sends match_end.
            await play_hand(ws, team, bot_label=bot)
    except websockets.exceptions.InvalidStatusCode as exc:
        if exc.status_code in {502, 503}:  # typical cold-start codes on Render/Fly free tiers
            LOGGER.error("Server reported %s (service warming up?). Wait 30s and retry.", exc.status_code)
        else:
            LOGGER.error("Failed to connect: %s", exc)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chaos Poker Bot client")
    parser.add_argument("--team", required=True, help="Team name registered with the host")
    parser.add_argument("--url", default="ws://127.0.0.1:9876/ws", help="WebSocket URL")
    parser.add_argument("--log-level", default="INFO")

    def _bot(value: str) -> str:
        result = value.strip().upper()
        if result not in {"A", "B"}:
            raise argparse.ArgumentTypeError("--bot must be A or B")
        return result

    parser.add_argument(
        "--bot",
        type=_bot,
        help="Optional practice slot (A or B) to enable in-server A/B testing",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    LOGGER.setLevel(getattr(logging, args.log_level.upper(), logging.INFO))
    asyncio.run(run_bot(args.team, args.url, bot=args.bot))


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def register_seat(state: Dict[str, Any], seat: Optional[int], team: Optional[str]) -> None:
    """Remember which team is sitting in each seat so logs can use names."""

    if seat is None or team is None:
        return
    state.setdefault("seat_map", {})[seat] = team


def render_card(card: str) -> str:
    """Return a card such as 'Ah' rendered with a unicode suit if enabled."""

    if USE_UNICODE_CARDS and len(card) == 2 and card[1] in SUIT_SYMBOLS:
        return card[0] + SUIT_SYMBOLS[card[1]]
    return card


def render_cards(cards: list[str]) -> str:
    """Render a sequence of cards for logging."""

    if not cards:
        return "--"
    return " ".join(render_card(card) for card in cards)


if __name__ == "__main__":
    main()