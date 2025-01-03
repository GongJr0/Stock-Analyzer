from .Portfolio import Portfolio
from .ReturnPred import ReturnPred
from .nCrEngine import nCrEngine
from .Sentiment import Sentiment
from .PortfolioCache import PortfolioCache
from .CustomTypes import nCrResult

import datetime as dt
from operator import itemgetter

import pandas as pd
import yfinance as yf

import numpy as np
from scipy.optimize import minimize


from typing import Optional, Any
from os import PathLike

from tqdm.auto import tqdm


class nCrOptimize(nCrEngine):
    """Find the optimal portfolio for a target return in a combination pool created from index components."""
    def __init__(self,
                 market: str,
                 n: int,
                 target_return: float = 0.1,
                 horizon: int = 21,
                 lookback: int = 252,
                 max_pool_size: Optional[int] = None,
                 api_key_path: Optional[PathLike] = None,
                 api_var_name: Optional[str] = None) -> None:

        super().__init__(market, n, horizon, lookback, max_pool_size, target_return)

        self.key_path = api_key_path
        self.key_var = api_var_name

        self.portfolio_cache = PortfolioCache()
        self.portfolios = self._get_portfolios()

        self.market_returns = self._get_market().tz_localize(None)

    def _get_portfolios(self) -> list:
        """
        Get Portfolio objects from string combinations.
        """
        portfolios = []
        for comb in self.ncr_gen:
            portfolio = Portfolio(*comb)
            portfolios.append(portfolio)
        return portfolios

    def _get_market(self) -> pd.Series:
        """Get the market returns for the given horizon."""
        start = dt.datetime.today() - dt.timedelta(days=self.lookback)
        start = start.date()
        end = dt.datetime.today().date()

        market_close = yf.Ticker(self.market).history(start=start, end=end)["Close"]
        market_returns = (market_close - market_close.shift(self.horizon)) / market_close.shift(self.horizon)
        market_returns = market_returns.dropna()

        return market_returns

    def _iteration_optimize(self, portfolio, bounds: tuple[float, float] = (0.0, 1.0)) -> dict[str, Any]:
        """Optimization function ran in parallel iteration of portfolios.

        :param portfolio: Portfolio object
        :return: tuple of Portfolio object and optimized weights
        """
        # Cache query
        cached = self.portfolio_cache.get(portfolio, self.target, bounds)
        if cached:
            return cached

        # Get the historical data for the portfolio
        periodic_returns = self.periodic_returns.loc[:, list(portfolio)]
        historical_close = self.historical_close.loc[:, list(portfolio)]

        return_preds = ReturnPred(historical_close, self.horizon).all_stocks_pred(comb=True)
        expected_returns = np.array([return_dict['expected_return'] for return_dict in return_preds.values()])

        if self.key_path and self.key_var:
            sentiment = Sentiment(self.key_path, self.key_var)
            sentiment_period = min(30, self.horizon)

            sentiment_adjustment = [sentiment.get_sentiment(stock, 3, sentiment_period) for stock in portfolio]
            expected_returns = expected_returns * (1 + 0.33 * np.array(sentiment_adjustment))

        cov_matrix = periodic_returns.cov()

        betas = np.array([
            np.cov(
                periodic_returns[stock].align(self.market_returns, join='inner')[0],  # Align the two series
                self.market_returns.align(periodic_returns[stock], join='inner')[0]
            )[0, 1] / np.var(self.market_returns.align(periodic_returns[stock], join='inner')[0])
            for stock in portfolio
        ])

        # Constraints
        def check_sum(weights):
            return np.sum(weights) - 1

        def target_return(weights):
            return np.dot(weights, expected_returns) - self.target

        initial_guess = np.array([1/len(portfolio) for _ in range(len(portfolio))])
        bounds_ls = [bounds for _ in range(len(portfolio))]
        constraints = [{'type': 'eq', 'fun': check_sum},
                       {'type': 'ineq', 'fun': target_return}]  # 'ineq' will produce return >= target as a constraint

        # Objective function
        def objective(weights):
            return weights.T @ cov_matrix @ weights

        result = minimize(objective, initial_guess, bounds=bounds_ls, constraints=constraints)  # type: ignore

        out = {
            "portfolio": " - ".join(portfolio),
            "weights": result.x.round(4).tolist(),  # Convert to list
            "return": np.dot(result.x, expected_returns).round(4),
            "portfolio_variance": result.fun.round(4),
            "expected_returns": expected_returns.tolist(),  # Convert to list
            "cov_matrix": cov_matrix.values.tolist(),  # Convert to a nested list
            "betas": betas
        }

        self.portfolio_cache.cache(portfolio, self.target, bounds, out)
        return out

    def optimize_space(self, bounds: tuple = (0.0, 1.0)) -> nCrResult:
        """
        Optimize the combination space.

        :return: List of optimized portfolios (best to worst)
        """
        results = nCrResult([self._iteration_optimize(portfolio, bounds)
                            for portfolio in tqdm(self.portfolios, desc="Iterating over portfolios")])

        results.sort(key=lambda x: (x['return'], -x['portfolio_variance']), reverse=True)

        return results
