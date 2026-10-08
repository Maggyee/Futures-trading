"""Deliver the audited sequential comparison with the original indicator charts."""

import json
import os
from collections import Counter
from datetime import datetime, timedelta
import gzip
from pathlib import Path

import plotly.graph_objects as go

import build_trade_review as charts
from research.coverage_audit import rows
from research.data import file_sha256
from research.feature_cache import read_frames
from research.reporting import write_json
from research.storage import SpaceBudget

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parents[1]


def unfilled_confirmations(assessment,plan):
    cases=[]
    for scenario in assessment['scenarios']:
        if not scenario['variant'].startswith('confirmation'):
            continue
        run=Path(scenario['directory'])
        confirmed={}
        for r in rows(run/'confirmation_setups.csv.gz'):
            if r['confirmed']=='True':
                confirmed[(r['contract'],r['time'],r['direction'])]=json.loads(r['detail'])
        pending=[]
        for r in rows(run/'signals.csv.gz'):
            identity=(r['contract'],r['time'],r['direction'])
            if identity in confirmed and r['filled']=='False':
                pending.append(r|{'confirmation_detail':confirmed[identity]})
        assert len(pending)==len(confirmed)-sum(
            1 for t in scenario['trades']
            if (t['contract'],t['entry_signal_time'],t['direction']) in confirmed)
        if not pending:
            continue
        needed={(r['contract'],r['date']) for r in pending}
        ref=json.loads((run/'data_reference.json').read_text())
        source=(run/ref['object']).resolve()
        assert file_sha256(source)==ref['sha256']
        raw={}
        with gzip.open(source,'rt') as stream:
            next(stream)
            for line in stream:
                item=json.loads(line);r=item['row']
                assert r['trading_day']<=scenario['window']['end']
                key=r['symbol']+'.'+r['exchange']
                if item['kind']=='bar' and (key,r['trading_day']) in needed:
                    raw[(key,r['datetime'])]=r
        evidence=json.loads((run/'prepared_source_review.json').read_text())
        assert file_sha256(evidence['cache'])==evidence['cache_sha256']
        frames={}
        for key,n,records in read_frames(evidence['cache'],evidence['cache_key']):
            if n==1 and key in {k for k,_ in needed}:
                frames[key]=records
        for row in pending:
            clock=datetime.fromisoformat(row['time'])
            rr=[r for r in frames[row['contract']] if r['day']==row['date'] and clock-timedelta(minutes=25)<=datetime.fromisoformat(r['end'])<=clock+timedelta(minutes=15)]
            for r in rr:
                for field in ('open','high','low','close'):
                    assert r[field]==raw[(row['contract'],r['datetime'])][field]
            x=[r['end'][:19] for r in rr]
            f=go.Figure(go.Candlestick(x=x,open=[r['open'] for r in rr],high=[r['high'] for r in rr],low=[r['low'] for r in rr],close=[r['close'] for r in rr],name='真实1分钟K线',increasing_line_color='#d95d58',decreasing_line_color='#259581'))
            for ma,color in [(10,'#d99b13'),(20,'#6b5bd1')]:
                f.add_trace(go.Scatter(x=x,y=[r['ma'+str(ma)] for r in rr],mode='lines',name='MA'+str(ma),line=dict(color=color,width=1.8)))
            setup=row['confirmation_detail']['confirmation']
            w=setup['window']
            f.add_shape(type='rect',xref='x',yref='y domain',x0=w['armed_at'][:19],x1=w['expires_at'][:19],y0=0,y1=1,
                        fillcolor='rgba(152,74,200,.07)',line_width=0,layer='below')
            if setup['touch_event']:
                touched=next(r for r in rr if r['end']==setup['touch_event'])
                f.add_trace(go.Scatter(x=[setup['touch_event'][:19]],y=[touched['low'] if row['direction']=='LONG' else touched['high']],
                                      mode='markers',name='资格之后的MA10回踩',marker=dict(symbol='square-open',size=13,color='#984ac8')))
            f.add_trace(go.Scatter(x=[setup['setup_end'][:19],row['time'][:19]],y=[setup['confirmation_level']]*2,mode='lines+markers',name='形态建立极值／延续确认价',line=dict(color='#984ac8',dash='dot'),marker=dict(symbol='circle-open',size=12)))
            signal_bar=raw[(row['contract'],(clock-timedelta(minutes=1)).isoformat())]
            f.add_trace(go.Scatter(x=[row['time'][:19]],y=[signal_bar['close']],mode='markers',name='已完成的价格延续确认（未成交）',marker=dict(symbol='diamond-open',size=14,color='#2563eb',line_width=2)))
            reasons=json.loads(row['risk_rejections'])
            names={'single_trade_risk':'单笔风险预算','group_risk':'分组风险预算','portfolio_risk':'组合风险预算','group_margin':'分组保证金','margin':'总保证金','minimum_open_lots':'最小开仓手数','max_positions':'持仓名额'}
            cost=json.loads(row['cost_check']) if row.get('cost_check') else None
            if row['trigger']=='False' and cost and not cost['accepted']:
                reason=f"价格形态已确认，但成本／1分钟ATR为{cost['cost_atr']:.4f}，超过原上限{cost['max_cost_atr']:.2f}，没有触发开仓"
            elif reasons:
                reason='、'.join(names.get(r,r) for r in reasons)+'不足，未预约开仓'
            else:
                reason='预约后取消'
            if row.get('fill_price_check'):
                check=json.loads(row['fill_price_check'])
                if not check['accepted']:
                    reason='下一可用开盘的拟定成交价越过冻结的价格边界，已取消'
                    f.add_trace(go.Scatter(x=[check['opening_time'][:19]],y=[check['modeled_price']],mode='markers',name='越界的拟定成交价（已取消）',marker=dict(symbol='x-open',size=14,color='#a64738')))
                    f.add_hline(y=check['modeled_price_limit'],line=dict(color='#64748b',dash='dash'),annotation_text='冻结的允许成交价格边界')
            elif row.get('fill_cost_check') and not json.loads(row['fill_cost_check'])['accepted']:
                reason='成交前成本复核未通过'
            f.update_layout(template='plotly_white',height=520,margin=dict(t=75,b=55,l=65,r=35),legend=dict(orientation='h',y=1.14),font=dict(family='Noto Sans CJK SC,sans-serif'),hovermode='x unified',xaxis=dict(type='date',tickformat='%H:%M',rangeslider_visible=False))
            cases.append({'uid':scenario['variant']+'-'+row['contract']+'-'+row['time'],'contract':row['contract'],'time':row['time'],'direction':row['direction'],'channel':setup['kind'],'reason':reason,'triggered':row['trigger']=='True','cost_check':cost,'filled':False,'figure':json.loads(f.to_json()),'source_sha256':ref['sha256']})
    return sorted(cases,key=lambda c:(c['time'],c['contract']))


def main():
    os.umask(0o077)
    plan = json.loads((HERE/'plan.json').read_text())
    output = Path(plan['output'])
    assessment = json.loads((output/'assessment.json').read_text())
    if assessment['status'] != 'completed':
        raise RuntimeError('等待完整独立阶段及最终评估')
    report = {
        'created':assessment['created'],'scenarios':assessment['scenarios'],
        'assessment':assessment,'labels':assessment['labels'],'research_days':assessment['research_days'],
        'sample_note':'7月、8月及9月14—23日均为已经查看的开发窗口。结果已扣手续费和双边不利滑点；9月24—30日锁定测试未读取。',
    }
    data = charts.collect(report=report,coverage={'totals':assessment['totals']},
                          labels=assessment['labels'],focus=assessment['selected'],diagnose=False)
    data.update(assessment=assessment,selected=assessment['selected'],plan_sha256=file_sha256(HERE/'plan.json'))
    evidence = []
    for s in assessment['scenarios']:
        run = Path(s['directory'])
        proof = json.loads((run/plan['audit_filename']).read_text())
        channel_counts = Counter()
        if (run/'confirmation_setups.csv.gz').exists():
            for row in rows(run/'confirmation_setups.csv.gz'):
                detail = json.loads(row['detail'])
                if detail['setup']:
                    channel_counts[detail['setup']['kind']+'_setups'] += 1
                if row['confirmed']=='True':
                    channel_counts[detail['confirmation']['kind']+'_confirmed'] += 1
        for t in s['trades']:
            channel_counts[json.loads(t['entry_snapshot']).get('entry_channel','legacy')+'_fills'] += 1
        evidence.append({'month':s['month'],'variant':s['variant'],
                         'audit_status':proof['status'],'pattern_counts':dict(channel_counts),
                         'checks':proof.get('optimization_checks',{}).get('journal',{}),
                         'diagnostics':s['diagnostics']})
    data['stage_evidence'] = evidence
    data['unfilled_confirmations'] = unfilled_confirmations(assessment,plan)
    by_entry = {(c['trade']['contract'],c['trade']['direction'],c['trade']['entry_time']):c['trade']['uid']
                for c in data['charts'] if c['trade']['variant']=='control'}
    for c in data['charts']:
        t=c['trade']
        if t['variant']!='control':
            t['paired_uid']=by_entry.get((t['contract'],t['direction'],t['entry_time']))
        if t['entry_snapshot'].get('price_confirmation'):
            detail=t['entry_snapshot']['price_confirmation']
            c['entry_explanation']=(('突破' if t['entry_channel']=='confirmed_breakout' else 'MA10回踩')+
                                   '建立后，紧邻下一根完成K线收盘越过建立K线的极值。')
        else:
            c['entry_explanation']='原入场通道：全部过滤条件从不通过变为通过。'
        c['initial_stop_explanation']=('以三根完成5分钟K线的结构边界确定距离，并保留原ATR、成本下限，手数按原风险预算缩减。'
                                        if 'structure_anchor' in t['entry_protection'] else
                                        '原保护距离：训练跳数、1分钟ATR和成本下限取最大值。')
    structure_by_entry={(c['trade']['contract'],c['trade']['direction'],c['trade']['entry_time']):c['trade']
                        for c in data['charts'] if c['trade']['variant']=='structure'}
    data['structure_comparison']=[]
    for c in data['charts']:
        b=c['trade']
        if b['variant']!='control':
            continue
        t=structure_by_entry.get((b['contract'],b['direction'],b['entry_time']))
        fields=('uid','quantity','stop_5m_atr','holding_minutes','exit_reason','net_pnl','stop_price','target_price')
        data['structure_comparison'].append({'contract':b['contract'],'direction':b['direction'],'entry_time':b['entry_time'],
            'before':{k:b[k] for k in fields},'after':{k:t[k] for k in fields} if t else None,
            'net_delta':t['net_pnl']-b['net_pnl'] if t else -b['net_pnl']})
    data['failure_explanation']={
        'confirmed_patterns':sum(e['checks'].get('confirmed_patterns',0) for e in evidence if e['variant']=='confirmation'),
        'supplemental_fills':sum(e['pattern_counts'].get('confirmed_breakout_fills',0)+e['pattern_counts'].get('confirmed_pullback_fills',0)
                                 for e in evidence if e['variant']=='confirmation'),
        'best_trade_share_of_net':assessment['totals']['control']['best_trade']/assessment['totals']['control']['net'],
        'structure_shared_entries':sum(r['after'] is not None for r in data['structure_comparison']),
    }
    write_json(charts.OUT/'stage_evidence.json',evidence,SpaceBudget(plan['budget']))
    write_json(charts.OUT/'report_sources.json',{
        'assessment_sha256':file_sha256(output/'assessment.json'),
        'plan_sha256':file_sha256(HERE/'plan.json'),'builder_sha256':file_sha256(HERE/'build_trade_review.py'),
        'report_builder_sha256':file_sha256(__file__),'locked_test_read':False,
    },SpaceBudget(plan['budget']))
    charts.dump(charts.OUT/'review_data.json',data)
    charts.build(data,destination=charts.OUT/'structure_followup_review.html')


if __name__ == '__main__':
    main()
