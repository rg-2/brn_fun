## Consists of multiple parts

### Data downloader
1. Data provider will be Oanda (I have account and api token), will be contained in a secrets file.
2. Download currency data and store locally (maybe sql light)
3. List of symbols taken from config file
4. default will be 15 min bars, could be configurable, but software can convert the 15 min to larger bars if necessary
5. This has pretty much been done already, we can borrow from other projects specifically the forex_reversion data downloader

### Strategy
1. Looking to trade bounces as price reachs round numbers for the first time in a 'while'
2. Will use analysis tool to refine entries and exits
3. Looking for only high quality trades, will pick the best amongst the Forex universe
4. Should we trade blind, or wait for confirmation?
5. What is the average bounce, we can use this to determine ideal exits, and exit strategy?
6. What are the round numbers we want to use?
7. Can we use AI/ML to improve probability?

### Strategy Tester, Analysis, Visualization
1. Tester will allow us to run the strategy and experiment with parameters using historical data we've downloaded and determine if we have a good strategy
2. The tester will return a list of possible trades, or taken trades.  The analysis tool will allow us to look more closely at those trades to understand what happend and how price reacted
3. Analysis and Visualization will help to understand price action as price approaches that value, can we use that to determine if a bounce is higher probability
6. Also want to be able to easily look at candlestick plots so see and internalize price during the approache and bounce, or failure to bounce
7. We should probably allow downloaded of more granualr price data near the bounces for closer study, 1 min or even tick charts.

### System Configuration
1. This will run on my 'dev' linux machine
2. Will be accessible on the trailsend-srv website, so I can run tests and analysis and see results there.
3. We can edit code, etc right in git hub, and use a publish action to test strategies, i think, unless there is a better way.

### Things to not forget
1. Is there a pattern after the touch? Confirmation bar, engulfing bar, pin-bar, bull nose, etc.
2. Should we look for long term trend continuation
3. Should stop be above/below the wick of the breaking candle?
4. Would be interesting to apply this to different levels, specifically, previous day high/low and previous week high / low provided there is some 'space' before the touch and bounce.