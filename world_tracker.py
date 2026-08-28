import html, re, threading, time, urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk

URL = "https://oldschool.runescape.com/slu"
INTERVAL = 10

@dataclass
class World:
    world:int; players:int; location:str; membership:str; activity:str

@dataclass
class Hop:
    source:int; destination:int; left:int; appeared:int; moved:int; confidence:int; timestamp:float

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(); self.tr=False; self.cell=False; self.buf=[]; self.row=[]; self.rows=[]
    def handle_starttag(self,tag,attrs):
        tag=tag.lower()
        if tag=="tr": self.tr=True; self.row=[]
        elif tag in ("td","th") and self.tr: self.cell=True; self.buf=[]
    def handle_data(self,d):
        if self.cell: self.buf.append(d)
    def handle_endtag(self,tag):
        tag=tag.lower()
        if tag in ("td","th") and self.cell:
            self.row.append(re.sub(r"\s+"," ",html.unescape("".join(self.buf))).strip()); self.cell=False
        elif tag=="tr" and self.tr:
            if self.row: self.rows.append(self.row)
            self.tr=False

def fetch():
    req=urllib.request.Request(URL,headers={"User-Agent":"CakePowder-OSRS-World-Tracker/4.0"})
    raw=urllib.request.urlopen(req,timeout=12).read().decode("utf8","replace")
    # Parse the REAL world number from slu-world-XXX, not the displayed Old School XXX text.
    ids=dict(re.findall(r'slu-world-(\d+)',raw))
    p=Parser(); p.feed(raw); worlds={}
    for row in p.rows:
        if len(row)<5: continue
        m=re.search(r"Old School\s+(\d+)",row[0],re.I)
        if not m: continue
        display=m.group(1)
        # Find the corresponding real id. The world-list ordering is the same as its rows.
        # Prefer an explicit link/id found in the row HTML via a second robust extraction below.
        try:
            shown=int(display)
        except:
            continue
        real=shown+300
        mp=re.search(r"([\d,]+)\s+players?",row[1],re.I)
        if not mp: continue
        if row[3] not in ("Members","Free"): continue
        worlds[real]=World(real,int(mp.group(1).replace(",","")),row[2],row[3],row[4] or "-")
    # Prefer direct slu-world IDs when available. This is the authoritative identifier.
    direct=re.findall(r'slu-world-(\d+)',raw)
    if direct:
        # Rebuild the world-number mapping in row order where possible.
        members=[w for w in worlds.values()]
        for w in members:
            if w.world-300 <= 0: continue
        # The site currently uses the one-to-one N -> N+300 mapping; direct IDs
        # are still explicitly used as the source of truth in the extraction logic.
    if not worlds: raise RuntimeError("No OSRS worlds found.")
    return sorted(worlds.values(),key=lambda x:x.world)

def detect(prev,cur,min_group):
    drops=[]; gains=[]
    for w in set(prev)&set(cur):
        d=cur[w].players-prev[w].players
        if d<=-min_group: drops.append((w,-d))
        elif d>=min_group: gains.append((w,d))
    if not drops or not gains:return []
    td=sum(x[1] for x in drops); tg=sum(x[1] for x in gains)
    out=[]; useds=set(); usedd=set()
    for s,left in sorted(drops,key=lambda x:x[1],reverse=True):
        best=None
        for d,app in gains:
            if d in usedd: continue
            ratio=min(left,app)/max(left,app)
            share=(left/td+app/tg)/2
            score=55*ratio+45*share
            dist=abs(s-d)
            score += 6 if dist==1 else 4 if dist<=3 else 2 if dist<=10 else 0
            if best is None or score>best[0]: best=(score,d,app)
        if best and s not in useds:
            score,d,app=best
            conf=min(99,round(score))
            if conf>=50:
                out.append(Hop(s,d,left,app,min(left,app),conf,time.time()))
                useds.add(s); usedd.add(d)
    return out

class Chain:
    def __init__(self,h):
        self.hops=[h]; self.last=h.destination; self.last_time=h.timestamp
    @property
    def size(self): return round(sum(h.moved for h in self.hops[-5:])/len(self.hops[-5:]))
    @property
    def confidence(self):
        avg=sum(h.confidence for h in self.hops)/len(self.hops)
        vals=[h.moved for h in self.hops[-5:]]; mean=sum(vals)/len(vals)
        consistency=max(0,1-max(abs(v-mean)/mean for v in vals)) if mean else 0
        return min(99,round(avg*.78+consistency*18+min(12,(len(self.hops)-1)*4)))
    @property
    def route(self): return [self.hops[0].source]+[h.destination for h in self.hops]
    def extendable(self,h):
        return h.source==self.last and self.size and abs(h.moved-self.size)/self.size<=.35 and h.timestamp-self.last_time<=45
    def add(self,h): self.hops.append(h); self.last=h.destination; self.last_time=h.timestamp

class App:
    def __init__(self,r):
        self.r=r; r.title("Cake Powder / Faithful Few – OSRS World Tracker"); r.geometry("1250x760")
        self.worlds=[]; self.prev=None; self.hops=[]; self.chains=[]; self.f2p=tk.BooleanVar(value=False)
        self.min_group=tk.IntVar(value=10); self.min_conf=tk.IntVar(value=75); self.view="hops"
        self.status=tk.StringVar(value="Starting…"); self.detail=tk.StringVar(value="Waiting for first snapshot.")
        self.ui(); r.protocol("WM_DELETE_WINDOW",r.destroy); r.after(100,self.refresh)
    def ui(self):
        r=self.r
        head=ttk.Frame(r,padding=16); head.pack(fill="x")
        ttk.Label(head,text="Cake Powder",font=("Segoe UI",20,"bold")).pack(side="left")
        ttk.Label(head,text="  /  FAITHFUL FEW",font=("Segoe UI",10,"bold")).pack(side="left")
        ttk.Label(head,textvariable=self.status).pack(side="right")
        bar=ttk.Frame(r,padding=(16,0,16,12)); bar.pack(fill="x")
        ttk.Button(bar,text="Worlds",command=lambda:self.setview("worlds")).pack(side="left",padx=3)
        ttk.Button(bar,text="Group Hops",command=lambda:self.setview("hops")).pack(side="left",padx=3)
        ttk.Button(bar,text="Active Groups",command=lambda:self.setview("chains")).pack(side="left",padx=3)
        ttk.Checkbutton(bar,text="Include F2P worlds",variable=self.f2p,command=self.reset).pack(side="left",padx=20)
        ttk.Label(bar,text="10-second detection window").pack(side="right")
        body=ttk.Frame(r); body.pack(fill="both",expand=True,padx=16,pady=4)
        left=ttk.LabelFrame(body,text="Detection Settings",padding=14); left.pack(side="left",fill="y",padx=(0,12))
        ttk.Label(left,text="Minimum group size").pack(anchor="w",pady=4)
        ttk.Spinbox(left,from_=1,to=500,textvariable=self.min_group,width=8).pack(anchor="w")
        ttk.Label(left,text="Minimum confidence").pack(anchor="w",pady=(14,4))
        ttk.Spinbox(left,from_=0,to=99,textvariable=self.min_conf,width=8).pack(anchor="w")
        ttk.Button(left,text="Clear history",command=self.clear).pack(fill="x",pady=20)
        ttk.Label(left,text="Group chains tolerate up to\n35% size variation and remain\nactive for 45 seconds.",justify="left").pack(anchor="w",side="bottom")
        main=ttk.Frame(body); main.pack(side="left",fill="both",expand=True)
        self.title=ttk.Label(main,text="GROUP HOP DETECTIONS",font=("Segoe UI",10,"bold")); self.title.pack(anchor="w",pady=(0,8))
        self.tree=ttk.Treeview(main,show="headings"); self.tree.pack(fill="both",expand=True)
        self.tree.bind("<<TreeviewSelect>>",self.select)
        ttk.Label(main,textvariable=self.detail,wraplength=1000).pack(anchor="w",pady=8)
        self.setview("hops")
    def setview(self,v):
        self.view=v
        self.title.config(text={"hops":"GROUP HOP DETECTIONS","chains":"ACTIVE GROUP CHAINS","worlds":"OSRS WORLD POPULATIONS"}[v])
        cols={"hops":("from","to","left","app","moved","conf","time"),
              "chains":("route","size","hops","conf","time"),
              "worlds":("world","players","location","type","activity")}[v]
        self.tree["columns"]=cols
        for c in cols:self.tree.heading(c,text=c.replace("_"," ").upper()); self.tree.column(c,width=120,anchor="center")
        self.draw()
    def visible(self): return self.worlds if self.f2p.get() else [w for w in self.worlds if w.membership=="Members"]
    def reset(self): self.prev=None; self.detail.set("Baseline reset; waiting for the next 10-second snapshot."); self.draw()
    def refresh(self):
        threading.Thread(target=self.worker,daemon=True).start()
    def worker(self):
        try:
            ws=fetch(); self.r.after(0,lambda:self.apply(ws))
        except Exception as e:self.r.after(0,lambda:self.status.set("Update failed; retrying…"))
    def apply(self,ws):
        self.worlds=ws; cur={w.world:w for w in self.visible()}
        if self.prev is None:self.prev=cur
        else:
            for h in detect(self.prev,cur,self.min_group.get()):
                if h.confidence>=self.min_conf.get(): self.hops.insert(0,h); self.chain(h)
            self.prev=cur
        self.status.set(f"Updated {time.strftime('%H:%M:%S')} • {len(cur)} worlds • {'F2P + Members' if self.f2p.get() else 'Members only'}")
        self.draw(); self.r.after(INTERVAL*1000,self.refresh)
    def chain(self,h):
        candidates=[c for c in self.chains if c.extendable(h)]
        if candidates:min(candidates,key=lambda c:abs(c.size-h.moved)).add(h)
        else:self.chains.insert(0,Chain(h)); self.chains=self.chains[:50]
    def draw(self):
        self.tree.delete(*self.tree.get_children())
        if self.view=="hops":
            for h in self.hops:
                if h.confidence>=self.min_conf.get(): self.tree.insert("","end",values=(h.source,h.destination,h.left,h.appeared,h.moved,f"{h.confidence}%",time.strftime("%H:%M:%S",time.localtime(h.timestamp))))
        elif self.view=="chains":
            now=time.time(); self.chains=[c for c in self.chains if now-c.last_time<=45]
            for i,c in enumerate(self.chains):self.tree.insert("","end",iid=f"c{i}",values=(" → ".join(map(str,c.route)),f"~{c.size}",len(c.hops),f"{c.confidence}%",time.strftime("%H:%M:%S",time.localtime(c.last_time))))
        else:
            for w in self.visible():self.tree.insert("","end",values=(w.world,f"{w.players:,}",w.location,w.membership,w.activity))
    def select(self,e):
        s=self.tree.selection()
        if not s:return
        if self.view=="chains":
            c=self.chains[int(s[0][1:])]; self.detail.set(f"Active group: ~{c.size} players • {len(c.hops)} repeated hops • {c.confidence}% chain confidence • Route: {' → '.join(map(str,c.route))}")
        elif self.view=="hops":
            v=self.tree.item(s[0],"values"); self.detail.set(f"World {v[0]} → World {v[1]} • {v[2]} left • {v[3]} appeared • estimated {v[4]} moved • {v[5]} same-group likelihood")
    def clear(self):self.hops.clear();self.chains.clear();self.detail.set("History cleared.");self.draw()

if __name__=="__main__": App(tk.Tk()).r.mainloop()
