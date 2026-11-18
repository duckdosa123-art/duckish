import chess

def test_promotion_parsing():
    """Test parsing of promotion SAN moves with check/mate."""
    tests = [
        ("axb1=Q+", "Capture promotion with check"),
        ("e8=N#", "Promotion to knight with mate"),
    ]
    
    for san, description in tests:
        try:
            board = chess.Board()
            move = board.parse_san(san)
            print(f"PASS: {san} ({description}) -> {move.uci()}")
        except Exception as e:
            print(f"FAIL: {san} ({description}) -> {type(e).__name__}: {e}")

if __name__ == "__main__":
    test_promotion_parsing()