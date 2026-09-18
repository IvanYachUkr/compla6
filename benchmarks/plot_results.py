"""Plot the measured size/decode trade-off without changing benchmark results."""
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, MultipleLocator

BASE = Path(__file__).resolve().parent / 'recorded/whole'
OUT = BASE/'plots'
OUT.mkdir(exist_ok=True)
rows = list(csv.DictReader((BASE/'comparison.csv').open()))
inputs = json.loads((BASE.parents[1]/'whole/inputs.json').read_text())
for row in rows:
    row['factor'] = inputs[row['dataset']]['bytes']/int(row['archive_bytes'])
    row['speed'] = float(row['decode_MB_s'])
    row['provisional'] = '†' in row['label']
    row['pareto'] = not any(
        other['dataset'] == row['dataset'] and
        int(other['archive_bytes']) <= int(row['archive_bytes']) and
        float(other['decode_MB_s']) >= row['speed'] and
        (int(other['archive_bytes']) < int(row['archive_bytes']) or float(other['decode_MB_s']) > row['speed'])
        for other in rows)

labels = {
    'yelp-lz4-1': ('LZ4-1', (12, 7), 'left'),
    'yelp-zstd-3': ('Zstd-3', (12, -24), 'left'),
    'yelp-zstd-19': ('Zstd-19', (12, 18), 'left'),
    'yelp-astra-compact': ('Astra Ultra\ncompact fast', (-15, -44), 'right'),
    'yelp-astra-density': ('Astra Ultra\nhigher compression', (-58, 48), 'right'),
    'yelp-glm-zstd': ('GLM\ncolumnizer + Zstd', (26, -5), 'left'),
    'yelp-sol-terra-dense': ('Sol plan → Terra\ndense', (-46, 18), 'right'),
    'yelp-glm-decode-structured': ('GLM new\nstructured', (48, 20), 'left'),
    'python-lz4-1': ('LZ4-1', (12, 15), 'left'),
    'python-zstd-3': ('Zstd-3', (-12, 18), 'right'),
    'python-zstd-19': ('Zstd-19', (12, 17), 'left'),
    'python-glm-fast': ('GLM\nfrom scratch, fast', (-15, 20), 'right'),
    'python-glm-zstd': ('GLM\ntuned Zstd', (-14, 16), 'right'),
    'python-terra-dense': ('Terra MAX\nfrom scratch, dense', (-37, 100), 'right'),
    'python-glm-decode-fast': ('GLM new\nfast mode', (-14, -30), 'right'),
    'python-glm-decode-size': ('GLM new\nsize mode†', (16, -10), 'left'),
    'python-glm-decode-zstd': ('GLM new\nZstd mode†', (16, 21), 'left'),
}


def style(row):
    name = row['id']
    if any(name.endswith(suffix) for suffix in ('lz4-1','zstd-3','zstd-19')):
        return '#2867B2', 's'
    if 'astra' in name:
        return '#7652BA', '^'
    if 'terra' in name:
        return '#B87516', 'D'
    return ('#CE453C' if '-decode-' in name else '#07847B'), 'o'


plt.rcParams.update({
    'font.family':'DejaVu Sans', 'font.size':13, 'axes.labelsize':15,
    'axes.edgecolor':'#75808B', 'axes.linewidth':0.9,
    'xtick.color':'#44505D', 'ytick.color':'#44505D', 'text.color':'#182635',
    'axes.labelcolor':'#182635', 'savefig.facecolor':'white',
    'svg.fonttype':'none', 'pdf.fonttype':42,
})

with PdfPages(OUT/'yelp-python-pareto.pdf') as pdf:
    for dataset, title in [('yelp','Yelp'), ('python','Python source')]:
        data = [row for row in rows if row['dataset'] == dataset]
        front = sorted((row for row in data if row['pareto']), key=lambda row:row['factor'])
        fig, ax = plt.subplots(figsize=(12.8, 8.6), dpi=180)
        fig.subplots_adjust(left=0.105,right=0.975,bottom=0.18,top=0.83)
        fig.text(0.105,0.947,title,fontsize=25,fontweight='bold',ha='left')
        fig.text(0.105,0.899,'Compression factor vs. decompression speed',fontsize=17,ha='left')
        fig.text(0.105,0.857,'Native RAM-to-RAM · 1 CPU core · median of 3 trials · original ≈ 100 MB',
                 fontsize=11.5,color='#596676',ha='left')
        ax.set_facecolor('#FBFCFE')
        ax.grid(True,color='#E2E7ED',linewidth=0.7,zorder=0)
        ax.spines[['top','right']].set_visible(False)
        ax.set_xlim((2,17.5) if dataset == 'yelp' else (1.6,6.85))
        ax.set_ylim(0,1300)
        ax.xaxis.set_major_locator(MultipleLocator(2 if dataset == 'yelp' else 1))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value,pos:f'{value:g}×'))
        ax.yaxis.set_major_locator(MultipleLocator(200))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value,pos:f'{value:,.0f}'))
        ax.set_xlabel('Compression factor = original bytes / archive bytes',labelpad=13)
        ax.set_ylabel('Decompression speed (MB/s)',labelpad=12)
        ax.plot([r['factor'] for r in front],[r['speed'] for r in front],
                color='#4C5866',lw=1.8,ls='-',zorder=2)
        for row in data:
            color, marker = style(row)
            ax.scatter(row['factor'],row['speed'],s=120 if row['pareto'] else 95,
                       marker=marker,facecolors='white' if row['provisional'] else color,
                       edgecolors=color if row['provisional'] else '#FFFFFF',linewidths=2,zorder=4)
            label, offset, align = labels[row['id']]
            ax.annotate(label,xy=(row['factor'],row['speed']),xytext=offset,
                        textcoords='offset points',ha=align,va='center',fontsize=11.7,
                        color='#243445',fontweight='bold' if row['pareto'] else 'normal',
                        bbox={'boxstyle':'round,pad=0.18','fc':'white','ec':'none','alpha':0.92},
                        arrowprops={'arrowstyle':'-','color':'#929CA8','lw':0.8,
                                    'shrinkA':4,'shrinkB':7},zorder=5)
        handles = [Line2D([0],[0],color='#4C5866',ls='-',lw=1.8,
                          label='Pareto frontier among measured methods')]
        if dataset == 'python':
            handles.append(Line2D([0],[0],marker='o',ls='none',markerfacecolor='white',
                                  markeredgecolor='#CE453C',markeredgewidth=1.8,markersize=8,
                                  label='† Provisional Lab qualification'))
        ax.legend(handles=handles,loc='upper left',frameon=False,fontsize=10.5,
                  borderaxespad=0.9,handlelength=2.6)
        ax.text(0.986,0.967,'Better ↗',transform=ax.transAxes,ha='right',va='top',
                fontsize=12,color='#596676')
        fig.text(0.105,0.082,'Archive includes framing and online dictionaries; decoder executables excluded.',
                 fontsize=10.5,color='#596676')
        fig.text(0.105,0.051,'All points passed byte-exact full-dataset RAM checks. Frontier uses these two metrics only.',
                 fontsize=10.5,color='#596676')
        if dataset == 'python':
            fig.text(0.105,0.023,'† Size mode failed a separate streaming gate; Zstd mode did not finish full Lab qualification.',
                     fontsize=9.8,color='#596676')
        fig.savefig(OUT/f'{dataset}-pareto.png',dpi=200)
        fig.savefig(OUT/f'{dataset}-pareto.svg')
        pdf.savefig(fig)
        plt.close(fig)

with (OUT/'plot-data.csv').open('w',newline='') as stream:
    writer = csv.DictWriter(stream,fieldnames=['dataset','id','label','factor','speed','pareto','provisional'],extrasaction='ignore')
    writer.writeheader();writer.writerows(rows)
print(json.dumps({'plots':str(OUT),'pareto':{d:[r['label'] for r in rows if r['dataset']==d and r['pareto']] for d in ('yelp','python')}},indent=2))
