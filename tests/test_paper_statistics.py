from decimal import Decimal
import json
import unittest

from arbitrage.paper_statistics import closed_statistics
from test_economics import NOW,HOUR


class PaperStatisticsTests(unittest.TestCase):
    def test_independent_closed_pnl_metrics_and_absolute_drawdown(self):
        pnl=[11,-3,-7,6]
        net=[10,-4,-8,5]
        rows=[(NOW+(i+1)*HOUR,NOW,str(pnl[i]),str(net[i])) for i in range(4)]
        result=closed_statistics(rows,4)
        self.assertEqual(result['max_drawdown_before_funding_usdt'],'10')
        self.assertEqual(result['max_drawdown_net_usdt'],'12')
        self.assertEqual(result['win_rate_pct'],'50')
        self.assertEqual(result['profit_factor'],'1.25')
        self.assertEqual(result['mean_net_usdt'],'0.75')
        self.assertEqual(result['best_net_usdt'],'10')
        self.assertEqual(result['worst_net_usdt'],'-8')
        self.assertEqual(result['mean_hold_minutes'],'150')
        self.assertEqual([Decimal(p['net_usdt']) for p in result['points']],[0,10,6,-2,3])

    def test_unknown_funding_breaks_cumulative_net_without_skipping_loss(self):
        rows=[(NOW+i*1000,NOW-1000,str(before),net) for i,(before,net) in enumerate([(5,'5'),(-3,None),(7,'7')])]
        result=closed_statistics(rows,3)
        self.assertEqual([p['net_usdt'] for p in result['points']],['0','5',None,None])
        for key in ('win_rate_pct','profit_factor','mean_net_usdt','best_net_usdt','worst_net_usdt','max_drawdown_net_usdt'):
            self.assertIsNone(result[key])
        self.assertEqual(result['max_drawdown_before_funding_usdt'],'3')
        self.assertEqual(result['profit_factor_reason'],'incomplete_funding')

    def test_same_timestamp_is_a_group_without_invented_intraday_drawdown(self):
        result=closed_statistics([(NOW,NOW-1000,'10','10'),(NOW,NOW-1000,'-10','-10')],2)
        self.assertEqual(result['close_groups'],1)
        self.assertEqual(result['max_drawdown_net_usdt'],'0')
        self.assertEqual([p['ordinal'] for p in result['points']],[0,2])

    def test_no_losses_no_trades_and_flat_are_not_infinite_or_unknown_zero(self):
        empty=closed_statistics([],0)
        self.assertEqual(empty['points'],[])
        self.assertIsNone(empty['win_rate_pct'])
        result=closed_statistics([(NOW,NOW,'0','0')],1)
        self.assertTrue(result['complete'])
        self.assertEqual(result['win_rate_pct'],'0')
        self.assertEqual(result['max_drawdown_net_usdt'],'0')
        self.assertIsNone(result['profit_factor'])
        self.assertEqual(result['profit_factor_reason'],'no_losses')
        json.dumps(result,allow_nan=False)

    def test_bounded_sample_preserves_short_peak_and_trough_and_exact_metrics(self):
        def rows():
            for i in range(5000):
                pnl=100 if i==450 else -150 if i==451 else 50 if i==452 else 0
                yield NOW+i*1000,NOW-1000,str(pnl),str(pnl)
        result=closed_statistics(rows(),5000)
        self.assertLessEqual(len(result['points']),281)
        self.assertTrue(result['sampled'])
        self.assertIn('100',[p['net_usdt'] for p in result['points']])
        self.assertIn('-50',[p['net_usdt'] for p in result['points']])
        self.assertEqual(result['max_drawdown_net_usdt'],'150')
        self.assertEqual(result['points'][-1]['ordinal'],5000)

    def test_sampling_keeps_first_unknown_boundary(self):
        rows=((NOW+i*1000,NOW-1000,'1',None if i==455 else '1') for i in range(5000))
        result=closed_statistics(rows,5000)
        first_unknown=next(p for p in result['points'] if p['net_usdt'] is None)
        self.assertEqual(first_unknown['ordinal'],456)
        self.assertTrue(all(p['net_usdt'] is None for p in result['points'] if p['ordinal']>=456))

    def test_reconciliation_rebuilds_whole_curve_and_restores_metrics(self):
        rows=[(NOW,NOW-1000,'2','2'),(NOW+1000,NOW-1000,'-3',None),(NOW+2000,NOW-1000,'4','4')]
        self.assertFalse(closed_statistics(rows,3)['complete'])
        rows[1]=(NOW+1000,NOW-1000,'-3','-3')
        fixed=closed_statistics(rows,3)
        self.assertTrue(fixed['complete'])
        self.assertEqual(fixed['points'][-1]['net_usdt'],'3')
        self.assertEqual(fixed['max_drawdown_net_usdt'],'3')

    def test_inconsistent_counts_or_timestamps_rejected(self):
        for rows,count in (([(NOW,NOW,'1','1')],2),([(NOW,NOW+1,'1','1')],1),([('bad',NOW,'1','1')],1)):
            with self.assertRaises(ValueError): closed_statistics(rows,count)
