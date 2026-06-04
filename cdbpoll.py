"""
   Script to monitor position PVs on chessdb.cn at regular intervals concurrently.
"""
import argparse, asyncio, time, cdblib, chess, re
from datetime import datetime


def get_core_fen(board):
    """Returns FEN string excluding move counters to detect transpositions."""
    return " ".join(board.fen().split()[:4])


def get_targets(base_fen, max_depth, includes, excludes, use_san):
    """
    Generates all board positions resulting from playing `max_depth` moves.
    Applies `includes` and `excludes` filters ONLY on the first ply.
    """
    if max_depth == 0:
        return [(base_fen, [])]

    targets = []
    def dfs(board, depth, move_seq):
        if depth == 0 or board.is_game_over():
            targets.append((board.fen(), move_seq))
            return
        
        is_first = (depth == max_depth)
        
        for m in list(board.legal_moves):
            m_san = board.san(m)
            m_uci = m.uci()
            
            # Apply inclusion/exclusion logic strictly on the 1st ply
            if is_first:
                if includes and (m_san not in includes and m_uci not in includes):
                    continue
                if excludes and (m_san in excludes or m_uci in excludes):
                    continue
            
            step_str = m_san if use_san else m_uci
            
            board.push(m)
            dfs(board, depth - 1, move_seq + [step_str])
            board.pop()
            
    dfs(chess.Board(base_fen), max_depth, [])
    return targets


def format_eval(e_val, target_turn, root_turn):
    """
    Formats the evaluation string and negates it if the target position's 
    turn is different from the root position's turn.
    """
    if e_val == "unknown" or e_val is None:
        e_str = "unknown"
        return " " * max(0, 6 - len(e_str)) + e_str

    flip = (target_turn != root_turn)
    
    if isinstance(e_val, int):
        val = -e_val if flip else e_val
        e_str = f"{val:4d}cp"
    else:
        e_str = str(e_val)
        if flip:
            if e_str.startswith('-'):
                e_str = e_str[1:] # e.g. "-M3" -> "M3"
            else:
                e_str = '-' + e_str # e.g. "M3" -> "-M3"
                
    # Pad to standard width
    if not e_str.endswith("cp"):
        e_str = " " * max(0, 6 - len(e_str)) + e_str
        
    return e_str


def get_eval_sort_key(e_val, target_turn, root_turn):
    """
    Converts mixed evaluations (cp or mates) into an absolute numerical score 
    from the perspective of the root position's side-to-move.
    """
    if e_val == "unknown" or e_val is None:
        return -float('inf')
        
    score = 0.0
    if isinstance(e_val, int):
        score = float(e_val)
    else:
        s = str(e_val).upper()
        match = re.search(r'-?\d+', s)
        if match:
            val = float(match.group())
            # Parse mate strings
            if 'M' in s or '#' in s or 'MATE' in s:
                if '-' not in s:
                    score = 100000.0 - val # Mate in 3 is better than Mate in 4
                else:
                    score = -100000.0 - val # Mated in 4 (-(-4)=+4 -> -99996) is better than Mated in 3 (-99997)
            else:
                score = val
                
    # Flip the perspective if the target board's turn differs from the root's turn
    if target_turn != root_turn:
        score = -score
        
    return score


class DisplayManager:
    """Manages appending outputs or rewriting terminal UI based on configuration."""
    def __init__(self, use_rewrite, targets):
        self.use_rewrite = use_rewrite
        self.lines = {}
        self.sort_keys = {}
        
        # Initialize default "Loading" states for dashboard mode
        if self.use_rewrite:
            for _, seq in targets:
                prefix = " ".join(seq)
                self.lines[prefix] = f"[{prefix}] Loading..."
                self.sort_keys[prefix] = -float('inf')
                
        self.dirty = True

    def update(self, prefix, display_str, sort_key):
        if not self.use_rewrite:
            # Traditional logging mode (ply = 0)
            print(display_str, flush=True)
            return
        
        # Dashboard rewrite mode (ply > 0)
        self.lines[prefix] = display_str
        self.sort_keys[prefix] = sort_key
        self.dirty = True

    async def draw_loop(self):
        if not self.use_rewrite:
            return
            
        while True:
            if self.dirty:
                self.draw()
                self.dirty = False
            await asyncio.sleep(0.2)

    def draw(self):
        if not self.use_rewrite:
            return
            
        # Clear screen completely to prevent tearing/ghosting from multiline PV wrapping
        print("\033[2J\033[H", end="")
        
        # Sort targets putting best options for the root side at the top
        sorted_keys = sorted(self.lines.keys(), key=lambda k: self.sort_keys[k], reverse=True)
        for k in sorted_keys:
            print(self.lines[k])


async def fetch_valid_response(cdb, epd, stable):
    """Polls chessdb until a valid response containing PV or OK status is returned."""
    while True:
        r = await (cdb.querypvstable(epd) if stable else cdb.querypv(epd))
        if isinstance(r, dict) and (r.get("status") in ("ok", "unknown") or "pv" in r):
            return r
        await asyncio.sleep(0.5)


async def get_full_pv(cdb, start_fen, args):
    """Fetches the PV and automatically merges continuations if it hits the 200-move cap."""
    base_r = await fetch_valid_response(cdb, start_fen, args.stable)
    e_val = cdblib.json2eval(base_r)
    
    current_moves = []
    current_fens = []
    board = chess.Board(start_fen)
    
    current_r = base_r
    while True:
        pv_raw = cdblib.json2pv(current_r, san=args.san)
        chunk_moves = pv_raw.split() if pv_raw else []
        
        if not chunk_moves:
            break
            
        for m_str in chunk_moves:
            current_moves.append(m_str)
            try:
                if args.san:
                    board.push_san(m_str)
                else:
                    board.push(chess.Move.from_uci(m_str))
                current_fens.append(get_core_fen(board))
            except Exception:
                current_fens.append(f"err_{m_str}")
        
        # Break if end of continuation line is reached
        if len(chunk_moves) < 200:
            break
            
        # Merge next 200-move continuation
        current_r = await fetch_valid_response(cdb, board.fen(), args.stable)

    return e_val, current_moves, current_fens


async def monitor_target(cdb, args, start_fen, move_seq, root_turn, display_mgr):
    """Independent monitoring loop for a single generated position."""
    COLORS = [
        "\033[38;5;196m", "\033[38;5;202m", "\033[38;5;208m", "\033[38;5;214m",
        "\033[38;5;220m", "\033[38;5;226m", "\033[38;5;154m", "\033[38;5;118m",
        "\033[38;5;46m",  "\033[38;5;43m",  "\033[38;5;39m"
    ]
    RESET = "\033[0m"

    last_printed_state = None    
    last_content = None  

    prefix_key = " ".join(move_seq)
    prefix_str = f"[{prefix_key}] " if prefix_key else "  "
    target_turn = chess.Board(start_fen).turn

    while True:
        e_val, current_moves, current_fens = await get_full_pv(cdb, start_fen, args)
        
        # Get correctly negated eval string based on the original root position's turn
        e_str = format_eval(e_val, target_turn, root_turn)
        current_content = (e_str, current_moves)
        
        if last_content is None or current_content != last_content:
            new_printed_state = []
            colored_moves = []
            
            for i, (m_str, f_str) in enumerate(zip(current_moves, current_fens)):
                if last_printed_state is None:
                    age = 10 
                else:
                    if i < len(last_printed_state) and f_str == last_printed_state[i]['fen']:
                        age = min(10, last_printed_state[i]['age'] + 1)
                    else:
                        age = 0 
                
                new_printed_state.append({'fen': f_str, 'age': age})
                colored_moves.append(f"{COLORS[age]}{m_str}{RESET}")

            colored_pv = " ".join(colored_moves)
            display_str = f"{prefix_str}{datetime.now().isoformat()}: {e_str} -- {colored_pv}"
            
            # Sort numerical key also accounts for root orientation 
            sort_key = get_eval_sort_key(e_val, target_turn, root_turn)
            display_mgr.update(prefix_key, display_str, sort_key)
            
            last_content = current_content
            last_printed_state = new_printed_state
        
        if args.sleep == 0:
            break
            
        await asyncio.sleep(args.sleep)


async def main():
    parser = argparse.ArgumentParser(
        description="Monitor dynamic changes in a position's PV on chessdb.cn by polling it at regular intervals.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--epd",
        help="FEN/EPD of the base position to monitor.",
        default="rnbqkbnr/pppppppp/8/8/6P1/8/PPPPPP1P/RNBQKBNR b KQkq -",
    )
    parser.add_argument(
        "--ply", 
        type=int, 
        default=0, 
        help="Generate target positions up to X plies deep from the given EPD. Enables dashboard redraw mode."
    )
    parser.add_argument(
        "--include", 
        nargs='*', 
        default=[], 
        help="Run script only on these specified moves at the first ply (SAN or UCI)."
    )
    parser.add_argument(
        "--exclude", 
        nargs='*', 
        default=[], 
        help="Ignore these specified moves at the first ply (SAN or UCI)."
    )
    parser.add_argument(
        "--concurrency", 
        type=int, 
        default=10, 
        help="Number of concurrent API connections to run at once."
    )
    parser.add_argument(
        "--stable", action="store_true", help='Pass "&stable=1" option to the API.'
    )
    parser.add_argument(
        "--sleep",
        type=int,
        default=3600,
        help="Time interval between polling requests in seconds.",
    )
    parser.add_argument(
        "--san", action="store_true", help="Give PV in short algebraic notation (SAN)."
    )
    parser.add_argument(
        "-u", "--user", help="Add this username to the http user-agent header."
    )
    parser.add_argument(
        "-s", "--suppressErrors", action="store_true", help="Suppress error messages from cdblib."
    )
    args = parser.parse_args()

    cdb = cdblib.cdbAPI(
        concurrency=args.concurrency, user=args.user, showErrors=not args.suppressErrors
    )
    
    # Validation loop on the root board
    while True:
        q = await cdb.queryscore(args.epd)
        if isinstance(q, dict):
            s = q.get("status")
            if s in ("ok", "unknown", "invalid board"):
                break
        await asyncio.sleep(0.5)

    if q.get("status") not in ("ok", "unknown"):
        print("  It is impossible to obtain a valid score for the given base position.")
        quit()

    root_board = chess.Board(args.epd)
    root_turn = root_board.turn

    # Generate branching targets based on ply depth and include/exclude rules
    targets = get_targets(args.epd, args.ply, args.include, args.exclude, args.san)
    
    if not targets:
        print("  No legal moves matched your constraints. Exiting.")
        quit()
        
    use_rewrite = (args.ply > 0)
    display_mgr = DisplayManager(use_rewrite, targets)
    
    if use_rewrite:
        print(f"Monitoring {len(targets)} position(s) concurrently...")
        time.sleep(1.5)

    # Launch background UI redraw task (does nothing if rewrite is disabled)
    draw_task = asyncio.create_task(display_mgr.draw_loop())

    # Spawn asynchronous background tasks for all identified target positions
    monitor_tasks = [monitor_target(cdb, args, fen, move_seq, root_turn, display_mgr) for fen, move_seq in targets]
    
    await asyncio.gather(*monitor_tasks)

    draw_task.cancel()
    
    # Final cleanup draw if exited quickly
    if use_rewrite and display_mgr.dirty:
        display_mgr.draw()

if __name__ == "__main__":
    asyncio.run(main())
