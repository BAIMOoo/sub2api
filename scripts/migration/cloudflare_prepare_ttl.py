#!/usr/bin/env python3
"""Lower only api.b4im.com's DNS TTL while retaining the live AWS address."""
import argparse
import datetime
import importlib.util
import json
import os
import pathlib
import tempfile
import urllib.error
import urllib.request

ZONE='ee0df4c2b558c519b22bc2dddddca611'
RECORD='e14e7e7d735bb542e19364a36e24199c'
API='https://api.cloudflare.com/client/v4/zones/'+ZONE+'/dns_records/'+RECORD


def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
 if not args.apply:
  print(json.dumps({'apply':False,'name':'api.b4im.com','origin_retained':'13.231.143.136','new_ttl':60,'origin_changed':False}));return
 spec=importlib.util.spec_from_file_location('cf_private',pathlib.Path(__file__).with_name('cloudflare_inspect.py'))
 cf=importlib.util.module_from_spec(spec);spec.loader.exec_module(cf)
 folder=pathlib.Path.home()/'.config/sub2api-migration'
 token=cf.load_token(folder/'cloudflare.env')
 def request(method='GET',body=None):
  req=urllib.request.Request(API,data=json.dumps(body).encode() if body is not None else None,method=method,
   headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
  try:
   with urllib.request.urlopen(req,timeout=20) as response:answer=json.load(response)
  except urllib.error.HTTPError as e:raise RuntimeError('DNS TTL preparation HTTP '+str(e.code)) from None
  if not answer.get('success'):raise RuntimeError('DNS TTL preparation refused by Cloudflare')
  return answer['result']
 def validate(row):
  if any(row.get(k)!=v for k,v in {'id':RECORD,'name':'api.b4im.com','type':'A','content':'13.231.143.136','proxied':False}.items()):
   raise RuntimeError('DNS state changed; refusing TTL update')
 before=request();validate(before)
 if before['ttl']!=60:request('PATCH',{'ttl':60})
 after=request();validate(after)
 if after['ttl']!=60:raise RuntimeError('DNS TTL readback mismatch')
 keys=('id','name','type','content','ttl','proxied','modified_on')
 evidence={'prepared_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'before':{k:before.get(k) for k in keys},
  'after':{k:after.get(k) for k in keys},'origin_changed':False}
 fd,tmp=tempfile.mkstemp(prefix='.dns-ttl-',dir=str(folder));os.fchmod(fd,0o600)
 with os.fdopen(fd,'w') as out:json.dump(evidence,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
 os.replace(tmp,folder/'cloudflare-ttl-prepared.json')
 print(json.dumps(evidence,indent=2))

if __name__=='__main__':main()
