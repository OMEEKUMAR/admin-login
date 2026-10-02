import os, re, json, hashlib, time, logging
from io import BytesIO
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
import firebase_admin
from firebase_admin import credentials, db

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

DB_URL = os.environ.get('FIREBASE_DATABASE_URL', 'https://shreeji-janseva-kendra-default-rtdb.asia-southeast1.firebasedatabase.app')
SERVICE_JSON = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', '')
TIMEOUT = int(os.environ.get('FETCH_TIMEOUT', '30'))
MAX_CANDIDATES_PER_SOURCE = int(os.environ.get('MAX_CANDIDATES_PER_SOURCE', '30'))

DEFAULT_SOURCES = [
    {'name':'UPSC Exam Notifications','url':'https://www.upsc.gov.in/hi/exams-related-info/exam-notification','kind':'jobs'},
    {'name':'SSC Official Notice Board','url':'https://ssc.gov.in/','kind':'mixed'},
    {'name':'Indian Army Recruitment','url':'https://joinindianarmy.nic.in/','kind':'jobs'},
    {'name':'Indian Navy Recruitment','url':'https://www.joinindiannavy.gov.in/','kind':'mixed'},
    {'name':'Indian Air Force / AFCAT','url':'https://afcat.cdac.in/AFCAT/','kind':'jobs'},
    {'name':'RRB / Railway Recruitment','url':'https://www.rrbapply.gov.in/','kind':'jobs'},
    {'name':'NTA Notice Board','url':'https://www.nta.ac.in/NoticeBoardArchive','kind':'mixed'},
    {'name':'UPSSSC Official','url':'https://upsssc.gov.in/','kind':'jobs'},
    {'name':'India Post GDS','url':'https://indiapostgdsonline.gov.in/','kind':'jobs'},
]

SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent':'Mozilla/5.0 (compatible; ShriJiJansevaAutoUpdater/1.0; +https://shreeji-janseva-kendra.web.app)',
    'Accept-Language':'en-IN,en;q=0.8,hi;q=0.7',
})

SECTION_ALIASES = {
    'importantDates':['important dates','important date','dates to remember','important events','key dates','schedule'],
    'applicationFee':['application fee','application fees','exam fee','fee details','application fees details'],
    'eligibility':['eligibility criteria','eligibility','educational qualification','qualification','education qualification','who can apply'],
    'howToApply':['how to apply','how to fill','how to apply online','application process','steps to apply','apply process','how to register'],
    'modeSelection':['mode of selection','selection process','selection procedure','selection criteria','selection stages'],
    'faqs':['faq','frequently asked questions','important questions'],
    'usefulLinks':['important links','some useful important links','useful links','direct links'],
    'vacancyDetails':['vacancy details','vacancy detail','post details','post wise vacancy','vacancy distribution'],
}

DATE_PAT = re.compile(r'\b(?:\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4}|\d{1,2}[\s-]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s,]+\d{4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s-]+\d{1,2}[,\s-]+\d{4})\b', re.I)


def now_ms(): return int(time.time()*1000)

def clean(s):
    s = re.sub(r'\u00a0|[\u200b-\u200d\ufeff]', ' ', str(s or ''))
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

def norm(s): return re.sub(r'[^a-z0-9\u0900-\u097f]+', ' ', clean(s).lower()).strip()

def canonical(url):
    p=urlparse(url)
    path=re.sub(r'/+$','',p.path or '/')
    return f'{p.scheme.lower()}://{p.netloc.lower()}{path}' + (('?'+p.query) if p.query else '')

def sha_key(text): return hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]

def fetch(url):
    last=None
    attempts=[(url,False),(f'https://r.jina.ai/{url}',True)]
    for target,is_jina in attempts:
        try:
            r=SESSION.get(target, timeout=TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            ctype=(r.headers.get('Content-Type') or '').lower()
            final=r.url
            if is_jina:
                # Reader output is normally markdown/text even when the original source is HTML/PDF.
                ctype='text/plain; jina-reader=1'
            return r.content, ctype, final
        except Exception as e:
            last=e
    raise last or RuntimeError('Source fetch failed')


def is_pdf(url, ctype=''):
    return 'pdf' in ctype or urlparse(url).path.lower().endswith('.pdf')

def html_visible_text(html):
    soup=BeautifulSoup(html, 'html.parser')
    for tag in soup(['script','style','noscript','svg','template']): tag.decompose()
    return clean(soup.get_text('\n', strip=True))

def html_links(html, base):
    soup=BeautifulSoup(html, 'html.parser')
    out=[]; seen=set()
    for a in soup.find_all('a', href=True):
        title=clean(a.get_text(' ', strip=True))
        url=urljoin(base, a.get('href'))
        if not title or not url or url in seen: continue
        if url.startswith(('javascript:','mailto:','tel:','#')): continue
        seen.add(url); out.append((title,url))
    return out

def markdown_links(text, base):
    out=[]; seen=set()
    for m in re.finditer(r'\[([^\]]{2,240})\]\((https?://[^)\s]+|/[^)\s]+)\)', str(text or '')):
        title=clean(m.group(1)); url=urljoin(base,m.group(2))
        if not title or not url or url in seen: continue
        seen.add(url); out.append((title,url))
    return out

def html_tables(html):
    soup=BeautifulSoup(html, 'html.parser')
    out=[]
    for table in soup.find_all('table'):
        rows=[]
        for tr in table.find_all('tr'):
            cells=[clean(c.get_text(' ',strip=True)) for c in tr.find_all(['th','td'])]
            if cells: rows.append(cells)
        if len(rows)>1: out.append(rows)
    return out

def pdf_text(data):
    reader=PdfReader(BytesIO(data))
    pages=[]
    for p in reader.pages:
        try: pages.append(p.extract_text() or '')
        except Exception: pages.append('')
    return clean('\n'.join(pages))[:50000]

def parse_document(data, ctype, final_url):
    if is_pdf(final_url, ctype):
        return {'text':pdf_text(data), 'links':[], 'tables':[]}
    raw=data.decode('utf-8','ignore')
    if 'jina-reader=1' in ctype:
        return {'text':clean(raw), 'links':markdown_links(raw, final_url), 'tables':[]}
    return {'text':html_visible_text(raw), 'links':html_links(raw, final_url), 'tables':html_tables(raw)}

def relevant(title, kind):
    t=norm(title)
    if kind=='jobs':
        return bool(re.search(r'\b(recruit|recruitment|vacan|job|application|advertisement|notification|post|officer|constable|agniveer|assistant|teacher|staff|engineer|apprentice|group|graduate|technician|clerk|navy|army|air force|afcat|gds)\b',t))
    return bool(re.search(r'\b(recruit|recruitment|vacan|job|application|advertisement|notification|post|admit|hall ticket|result|score|merit|selection|marks|answer key|certificate|intimation|agniveer|afcat)\b',t))

def category(title, kind):
    t=norm(title)
    if re.search(r'\b(admit|hall ticket|e admit|admit card|call letter|city intimation|intimation slip)\b',t): return 'Admit Cards'
    if re.search(r'\b(result|score card|scorecard|merit list|final marks|selection list|written result|answer key|cut off|cutoff)\b',t): return 'Results'
    return 'Latest Jobs'

def section(text, aliases, limit=120):
    lines=[clean(x) for x in text.splitlines() if clean(x)]
    aliases_n=[norm(a) for a in aliases]
    heads=set(norm(x) for arr in SECTION_ALIASES.values() for x in arr)
    for i,line in enumerate(lines):
        n=norm(line)
        if any(n==a or n.startswith(a+ ' ') or n.startswith(a+':') for a in aliases_n):
            out=[]
            tail=re.sub(r'^[^:–—-]{0,120}[:–—-]\s*','',line, count=1).strip()
            if tail and norm(tail)!=n: out.append(tail)
            for j in range(i+1,min(len(lines),i+1+limit)):
                if norm(lines[j]) in heads or any(norm(lines[j]).startswith(h+' ') for h in heads): break
                out.append(lines[j])
            return out
    return []

def pairs(lines):
    out=[]
    for l in lines:
        l=clean(l)
        if not l: continue
        m=re.match(r'^(.{2,100}?)\s*(?::|\||–|—|-|\s{2,})\s*(.{1,300})$',l)
        if m and len(m.group(2))>1:
            out.append({'label':clean(m.group(1)), 'value':clean(m.group(2))})
    return out

def extract_dates(text):
    sec=section(text, SECTION_ALIASES['importantDates'], 180)
    lines=sec or [l for l in text.splitlines() if DATE_PAT.search(l)]
    out=[]
    seen=set()
    for l in lines:
        if not DATE_PAT.search(l): continue
        m=re.search(r'^(.{1,130}?)(?:[:\-–—]|\s{2,})\s*(.*?\b(?:\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4}|\d{1,2}[ -](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ ,-]+\d{4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ -]\d{1,2}[, -]+\d{4})\b.*)$',l,re.I)
        if not m:
            m=re.search(r'^(.*?)(\b\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4}\b.*)$',l)
        if m:
            lab=clean(m.group(1)); val=clean(m.group(2))
        else:
            lab='Important Date'; val=l
        k=norm(lab)+'|'+norm(val)
        if k not in seen: seen.add(k); out.append({'label':lab or 'Date','value':val})
    return out[:60]

def extract_fee(text):
    sec=section(text, SECTION_ALIASES['applicationFee'], 120)
    lines=sec or [l for l in text.splitlines() if re.search(r'fee|rupees|₹|rs\.?|inr', l, re.I)]
    out=pairs(lines)
    if not out:
        for l in lines:
            if re.search(r'₹|\b(?:rs|inr)\.?\s*\d', l, re.I): out.append({'label':'Fee Details','value':l})
    return out[:50]

def extract_age(text):
    pats=[r'(?:age limit|age limit as on|age requirement|age criteria)\s*[:\-–—]?\s*([^\n]{3,220})',
          r'(?:minimum age)\s*[:\-–—]?\s*([^\n]{2,120})',
          r'(?:maximum age|max age)\s*[:\-–—]?\s*([^\n]{2,120})',
          r'\bage\s*[:\-–—]?\s*(\d{1,2}\s*(?:to|-|–)\s*\d{1,2}\s*years?)']
    for p in pats:
        m=re.search(p,text,re.I)
        if m: return clean(m.group(1))
    return ''

def extract_total(text):
    pats=[r'(?:total posts?|total vacancies?|total vacancy|number of posts?|number of vacancies?|no\.?\s*of\s*posts?|no\.?\s*of\s*vacancies?)\s*[:\-–—]?\s*([0-9][0-9,]*)\b']
    for p in pats:
        m=re.search(p,text,re.I)
        if m:return m.group(1)
    return ''

def table_rows(doc_tables, needles):
    nn=[norm(n) for n in needles]
    for rows in doc_tables:
        header=rows[0]; joined=' '.join(norm(c) for c in header)
        if any(n in joined for n in nn): return header,rows[1:]
    return None,None

def extract_vacancy_details(doc):
    header, rows=table_rows(doc['tables'],['post name','no of post','number of posts','vacancy details','designation'])
    if rows:
        out=[]
        for r in rows:
            if not r: continue
            out.append({'postName':r[0] if len(r)>0 else '', 'noOfPost':r[1] if len(r)>1 else '', 'state':r[2] if len(r)>2 else '', 'language':r[3] if len(r)>3 else ''})
        return [x for x in out if x['postName'] or x['noOfPost']][:100]
    sec=section(doc['text'],SECTION_ALIASES['vacancyDetails'],160)
    out=[]
    for l in sec:
        m=re.match(r'^(.{3,150}?)\s+(\d[\d,]*)\s*(?:posts?|vacancies?)?$',l,re.I)
        if m: out.append({'postName':clean(m.group(1)), 'noOfPost':m.group(2), 'state':'','language':''})
        elif len(l)>5: out.append({'postName':l,'noOfPost':'','state':'','language':''})
    return out[:100]

def extract_eligibility(doc):
    header, rows=table_rows(doc['tables'],['eligibility criteria','eligibility','educational qualification','qualification'])
    if rows:
        return [{'postName':r[0] if r else '', 'criteria':' | '.join(r[1:])} for r in rows if r][:100]
    sec=section(doc['text'],SECTION_ALIASES['eligibility'],180)
    return [{'postName':'Eligibility','criteria':x} for x in sec[:100]]

def extract_text_section(text, aliases):
    sec=section(text,aliases,220)
    return '\n'.join(sec[:220]).strip()

def extract_faqs(text):
    sec=section(text,SECTION_ALIASES['faqs'],250)
    out=[]; i=0
    while i<len(sec):
        q=clean(sec[i])
        if q.endswith('?') or re.match(r'^(q\.?|question|\d+[.)])',q,re.I):
            q=re.sub(r'^(?:q\.?|question|\d+[.)])\s*[:.)-]?\s*','',q,flags=re.I)
            a=[]; j=i+1
            while j<len(sec) and not sec[j].endswith('?') and not re.match(r'^(?:q\.?|question|\d+[.)])',sec[j],re.I):
                a.append(sec[j]); j+=1
            out.append({'q':q,'a':' '.join(a)})
            i=j
        else:i+=1
    return out[:80]

def links_to_fields(links, source_url):
    important=[]; check=[]; seen=set()
    for title,url in links:
        title=clean(title); url=urljoin(source_url,url)
        if not title or url in seen: continue
        seen.add(url)
        low=norm(title)
        if re.search(r'apply|application|registration|notification|advertisement|official|download|admit|result|answer key|syllabus|login|print|correction',low):
            important.append({'name':title,'url':url})
        if re.search(r'check|read more|related|syllabus|admit|result|answer key|merit|selection|exam pattern|notification|instructions',low):
            check.append({'name':title,'url':url})
    return important[:50],check[:30]

def extract_main_notice_link(links, base):
    ranked=[]
    for title,url in links:
        low=norm(title+' '+url)
        score=0
        if re.search(r'notification|advertisement|detailed|notice|prospectus',low): score+=8
        if url.lower().endswith('.pdf') or '.pdf?' in url.lower(): score+=6
        if re.search(r'application|apply|registration',low): score-=2
        ranked.append((score,title,url))
    ranked.sort(reverse=True)
    return ranked[0][2] if ranked else ''

def extract_structured(doc, links, source_url):
    text=doc['text']
    dates=extract_dates(text); fees=extract_fee(text); age=extract_age(text); total=extract_total(text)
    vdetails=extract_vacancy_details(doc); elig=extract_eligibility(doc)
    if not total and vdetails:
        nums=[]
        for v in vdetails:
            try: nums.append(int(re.sub(r'[^0-9]','',v.get('noOfPost',''))))
            except: pass
        if nums: total=str(sum(nums))
    imp, check=links_to_fields(links,source_url)
    return {
        'importantDates':dates,
        'applicationFee':fees,
        'ageLimit':age,
        'totalPost':total,
        'vacancyDetails':vdetails,
        'eligibility':elig,
        'checkLinks':check,
        'howToApply':extract_text_section(text,SECTION_ALIASES['howToApply']),
        'modeSelection':extract_text_section(text,SECTION_ALIASES['modeSelection']),
        'importantLinks':imp,
        'faqs':extract_faqs(text),
        'fullNotice':text[:40000],
        'sourceText':text[:40000],
        'description':text[:5000],
    }

def coverage(d):
    checks=[bool(d.get('importantDates')),bool(d.get('applicationFee')),bool(d.get('ageLimit')),bool(d.get('totalPost')),bool(d.get('vacancyDetails')),bool(d.get('eligibility')),bool(d.get('howToApply')),bool(d.get('modeSelection')),bool(d.get('importantLinks')),bool(d.get('faqs')),bool(d.get('fullNotice'))]
    return round(100*sum(checks)/len(checks))

def init_firebase():
    if not SERVICE_JSON:
        raise RuntimeError('FIREBASE_SERVICE_ACCOUNT_JSON secret is missing')
    cred=credentials.Certificate(json.loads(SERVICE_JSON))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred, {'databaseURL':DB_URL})

def load_sources():
    try:
        data=db.reference('autoUpdate/sources').get() or {}
        src=[]
        for k,v in data.items():
            if isinstance(v,dict) and v.get('enabled',True) and v.get('url'): src.append(v)
        if src:return src
    except Exception as e: logging.warning('Could not read sources from Firebase: %s',e)
    return DEFAULT_SOURCES

def upsert(src, candidate, detail_doc, detail_links, detail_url):
    title=clean(candidate[0])
    cat=category(title,src.get('kind','mixed'))
    d=extract_structured(detail_doc, detail_links, detail_url)
    key='auto_'+sha_key(src.get('name','')+'|'+canonical(candidate[1]))
    ref=db.reference('vacancies/'+key)
    old=ref.get() or {}
    record={
        'cat':old.get('cat') or cat,
        'title':title,
        'postDate':next((x['value'] for x in d['importantDates'] if DATE_PAT.search(x.get('value',''))), old.get('postDate','')),
        'intro':f"Official source update: {src.get('name','')}",
        'description':d['description'],
        'ageLimit':d['ageLimit'], 'totalPost':d['totalPost'],
        'importantDates':d['importantDates'], 'applicationFee':d['applicationFee'],
        'vacancyDetails':d['vacancyDetails'], 'eligibility':d['eligibility'],
        'checkLinks':d['checkLinks'], 'howToApply':d['howToApply'],
        'modeSelection':d['modeSelection'], 'importantLinks':d['importantLinks'], 'faqs':d['faqs'],
        'fullNotice':d['fullNotice'], 'sourceText':d['sourceText'],
        'mainLink':detail_url, 'sourceName':src.get('name',''), 'autoImported':True,
        'detailFetched':True, 'detailFetchedAt':now_ms(), 'detailCoverage':coverage(d),
        'updatedAt':now_ms(), 'createdAt':old.get('createdAt',now_ms()),
        'status':old.get('status','pending'),
    }
    ref.set(record)
    return key, record

def process_source(src):
    data,ctype,final=fetch(src['url'])
    listing=parse_document(data,ctype,final)
    candidates=[x for x in listing['links'] if relevant(x[0],src.get('kind','mixed'))][:MAX_CANDIDATES_PER_SOURCE]
    added=updated=failed=0
    for cand in candidates:
        try:
            ddata,dctype,dfinal=fetch(cand[1])
            detail=parse_document(ddata,dctype,dfinal)
            links=detail['links'][:]
            # Fetch linked official notification PDF/page when present.
            notice=extract_main_notice_link(links,dfinal)
            if notice and canonical(notice)!=canonical(dfinal):
                try:
                    ndata,nctype,nfinal=fetch(notice)
                    ndoc=parse_document(ndata,nctype,nfinal)
                    merged_links=links+ndoc['links']
                    if len(ndoc['text'])>len(detail['text']): detail=ndoc; dfinal=nfinal
                    else: detail['text'] += '\n\n'+ndoc['text']; detail['links']=merged_links; detail['tables'] += ndoc['tables']
                except Exception as e:
                    logging.info('Notice fetch skipped %s: %s',notice,e)
            key,record=upsert(src,cand,detail,listing['links']+detail['links'],dfinal)
            if record.get('createdAt')==record.get('updatedAt'): added+=1
            else: updated+=1
        except Exception as e:
            failed+=1; logging.warning('%s candidate failed: %s',cand[0],e)
    return {'source':src['name'],'candidates':len(candidates),'added':added,'updated':updated,'failed':failed}

def main():
    init_firebase()
    results=[]
    for src in load_sources():
        try: results.append(process_source(src))
        except Exception as e: results.append({'source':src.get('name'), 'error':str(e)}); logging.exception('Source failed')
    db.reference('autoUpdate/status').set({'lastRunAt':now_ms(),'lastRun':datetime.now(timezone.utc).isoformat(),'results':results})
    logging.info(json.dumps(results,ensure_ascii=False))

if __name__=='__main__': main()
