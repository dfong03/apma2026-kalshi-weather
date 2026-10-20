"""Record Kalshi order books (every level and size) for weather series.

One gzip JSON-lines file per series and UTC date: a line when a book changes, and a periodic heartbeat line.
Usage: kalshi-weather record [SERIES ...] [--interval SECONDS] [--data FOLDER] [--seconds N]
"""
from datetime import datetime,timezone
from pathlib import Path
import gzip
import json
import shutil
import signal
import time
import zlib
import httpx
from .exchange import HOSTS, PREFIX

BASE=HOSTS['prod']+PREFIX
ROOT=Path('data')
INTERVAL=2.;HEARTBEAT=300.;REFRESH=300.;FLOOR_GB=4.


def levels(side):
    """One side of a book as [price, contracts], worst first."""
    return [[round(float(price)*100),float(count)] for price,count in side or []]


def top(yes,no):
    """Best bid and ask for YES with their sizes; None for an empty side."""
    bid=max(yes,default=None);offer=max(no,default=None)
    return (bid[0] if bid else None,bid[1] if bid else None,100-offer[0] if offer else None,offer[1] if offer else None)


def due(book,last,now,heartbeat=HEARTBEAT):
    """Whether to write this book: it changed, or a heartbeat is due."""
    return last is None or book!=last[1] or now-last[0]>=heartbeat


def path(folder,ticker,now):
    return Path(folder)/f"kalshi_books_{ticker.split('-')[0]}"/f"{datetime.fromtimestamp(now,timezone.utc):%Y-%m-%d}.jsonl.gz"


def read(file):
    """Complete lines of one day's file."""
    rows=[]
    try:
        with gzip.open(file,'rt') as handle:
            for line in handle:
                try:rows.append(json.loads(line))
                except json.JSONDecodeError:break
    except (EOFError,OSError):pass
    return rows


def repair(file):
    """Rewrite a file cut short by a crash; returns the number of lines dropped."""
    if not file.exists():return 0
    data=file.read_bytes();text=b'';whole=True
    while data:
        member=zlib.decompressobj(31)
        try:text+=member.decompress(data)
        except zlib.error:whole=False;break
        if not member.eof:whole=False;break
        data=member.unused_data
    if whole:return 0
    lines=text.decode(errors='replace').split('\n');kept=[]
    for line in lines:
        try:json.loads(line);kept.append(line)
        except json.JSONDecodeError:continue
    spare=file.with_name(file.name+'.repairing')
    with gzip.open(spare,'wt') as handle:handle.write(''.join(line+'\n' for line in kept))
    spare.replace(file)
    return len([line for line in lines if line.strip()])-len(kept)


def open_tickers(client,series):
    out=[]
    for name in series:
        cursor=None
        while True:
            body=client.get(BASE+'/markets',params={'series_ticker':name,'status':'open','limit':200,**({'cursor':cursor} if cursor else {})}).raise_for_status().json()
            out+=[m['ticker'] for m in body.get('markets',[])];cursor=body.get('cursor')
            if not cursor or not body.get('markets'):break
    return sorted(out)


def record(series,interval=INTERVAL,folder=ROOT,seconds=None):
    client=httpx.Client(timeout=20);last={};files={};tickers=[];listed=0.;written=0;began=time.time();reported=began;failures=0;running=[True]
    for name in (signal.SIGINT,signal.SIGTERM):signal.signal(name,lambda *_:running.__setitem__(0,False))
    try:
        while running[0] and (seconds is None or time.time()-began<seconds):
            started=time.time()
            try:
                if started-listed>=REFRESH or not tickers:
                    tickers=open_tickers(client,series);listed=started;last={t:v for t,v in last.items() if t in tickers}
                    if shutil.disk_usage(folder).free<FLOOR_GB*1e9:print(f'stopping: less than {FLOOR_GB:g} GB of disk free',flush=True);break
                books=[]
                for i in range(0,len(tickers),100):
                    asked=time.time();body=client.get(BASE+'/markets/orderbooks',params=[('tickers',t) for t in tickers[i:i+100]]).raise_for_status().json();took=time.time()-asked
                    books+=[(asked+took,took,b['ticker'],levels(b['orderbook_fp'].get('yes_dollars')),levels(b['orderbook_fp'].get('no_dollars'))) for b in body.get('orderbooks',[])]
                failures=0
            except (httpx.HTTPError,KeyError,ValueError) as problem:   # keep going through outages
                failures+=1;print(f'{datetime.now(timezone.utc):%H:%M:%S} request failed ({type(problem).__name__}: {str(problem)[:120]}); retrying',flush=True);time.sleep(min(60,interval*2**min(failures,5)));continue
            for now,took,ticker,yes,no in books:
                if not due((yes,no),last.get(ticker),now):continue
                file=path(folder,ticker,now)
                if file not in files:
                    for old in [f for f in files if f.parent==file.parent]:files.pop(old).close()   # a new UTC date: close the old file
                    file.parent.mkdir(parents=True,exist_ok=True)
                    if (lost:=repair(file)):print(f'{file.name} was cut short by a crash; rewrote it without {lost} damaged line(s)',flush=True)
                    files[file]=gzip.open(file,'at')
                files[file].write(json.dumps([round(now,3),round(took,3),ticker,yes,no],separators=(',',':'))+'\n');last[ticker]=(now,(yes,no));written+=1
            for handle in files.values():handle.flush()
            if started-reported>=600:
                reported=started;size=sum(f.stat().st_size for f in files);print(f'{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {len(tickers)} markets, {written} lines written, today\'s files {size/1e6:.1f} MB',flush=True)
            time.sleep(max(0.,interval-(time.time()-started)))
    finally:
        for handle in files.values():handle.close()
        client.close()
    return written
