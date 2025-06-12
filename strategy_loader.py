# strategy_loader.py

def load_strategy(mode="bull"):
    if mode == "bull":
        from strategies.bull import should_buy, should_sell
    elif mode == "sideways":
        from strategies.sideways import should_buy, should_sell
    elif mode == "defensive":
        from strategies.defensive import should_buy, should_sell
    else:
        raise ValueError(f"Unknown strategy mode: {mode}")
    return should_buy, should_sell
