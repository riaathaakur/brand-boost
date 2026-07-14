{{--
  Branded tracking page — pure presentation.

  Contract: this template makes NO decisions. It receives $kit, the validated
  brand-kit JSON produced by the extraction pipeline (brandkit.py / its PHP
  port), decoded as an array. Every colour is an already-validated #rrggbb,
  every URL is already https-checked, every string is already length-capped.
  The kit is cached per seller (Redis/DB, ~30d TTL); rendering never triggers
  extraction.

  Escaping policy:
    {{ }}  everything by default (Blade escapes).
    {!! !!} ONLY for typography.font_face_css and logo.svg — both are
            generated/sanitised server-side by the pipeline (font_face_css is
            built from a validated charset; the SVG passed _sanitize_svg).
            Nothing user- or scrape-controlled reaches {!! !!} unsanitised.

  Controller sketch:
    $kit = Cache::remember("brandkit:$sellerId", now()->addDays(30),
             fn () => BrandKitService::fetch($sellerId));   // queue-filled
    return view('tracking', ['kit' => $kit, 'order' => $order]);
--}}
@php
    $c  = $kit['colors'];
    $t  = $kit['typography'];
    $order = $order ?? [
        'id'         => $kit['prefix'].'-3928104',
        'placed_on'  => '27th June, 2026',
        'eta'        => '28 Jun',
        'status'     => 'In Transit',
        'badge'      => 'On The Way',
        'event'      => 'Shipment In Transit',
        'event_desc' => 'Your package has left the sorting facility',
        'event_time' => '27 Jun 2026, 6:42 PM',
    ];
@endphp
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ $kit['brand_name'] }} — Track your order</title>
@if ($t['google_font'])
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family={{ str_replace(' ', '+', $t['google_font']) }}:wght@400;700;900&display=swap" rel="stylesheet">
@endif
<style>
{!! $t['font_face_css'] !!}
*{box-sizing:border-box;margin:0;padding:0}
body,*{font-family:{{ $t['body_font'] }}}
body{background:#e0e0e0;display:flex;justify-content:center}
.page{width:100%;max-width:430px;background:{{ $c['body_bg'] }};color:{{ $c['body_text'] }};min-height:100vh;overflow:hidden}
.ann{background:{{ $c['ann_bg'] }};color:{{ $c['ann_text'] }};font-size:11px;text-align:center;padding:9px 12px;letter-spacing:.03em}
.ann a{color:inherit;font-weight:700}
.hdr{display:flex;align-items:center;gap:12px;padding:14px 16px;background:{{ $c['header_bg'] }};border-bottom:{{ $c['header_border'] }}}
.hbg{width:22px;display:flex;flex-direction:column;gap:4px;flex-shrink:0}
.hbg span{height:2px;border-radius:2px;background:{{ $c['header_icon'] }}}
.hlogo{flex:1;text-align:center;min-width:0}
.hact{flex-shrink:0;width:22px}
section{padding:18px 16px}
.card{background:#fff;border-radius:10px;padding:16px;border-bottom:2px solid {{ $c['brand'] }};box-shadow:0 1px 4px rgba(0,0,0,.06)}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.lbl{font-size:10px;text-transform:uppercase;letter-spacing:.09em;opacity:.55;font-weight:700}
.val{font-size:13px;font-weight:700;margin-top:4px}
.div{height:1px;background:#eee;margin:14px 0}
.otp{display:flex;align-items:center;gap:12px;font-size:11px;opacity:.75}
.otp b{color:{{ $c['brand'] }};font-weight:800;white-space:nowrap;cursor:pointer}
.eta{font-size:28px;font-weight:900;margin-top:2px}
.st{font-size:22px;font-weight:900;margin-top:2px}
.status-badge{display:inline-block;margin-top:10px;background:{{ $c['brand'] }};color:#fff;font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;padding:6px 12px;border-radius:{{ $kit['cta_radius'] }}}
.tl{border-left:3px solid {{ $c['brand'] }};padding-left:12px;background:{{ $c['light'] }};padding:10px 12px;border-radius:0 6px 6px 0}
.tl-s{font-size:12px;font-weight:800}
.tl-d{font-size:11px;opacity:.7;margin-top:2px}
.tl-t{font-size:10px;opacity:.5;margin-top:3px}
.sec-t{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:2px}
.pimg{width:100%;aspect-ratio:3/4;object-fit:cover;display:block}
.pph{width:100%;aspect-ratio:3/4;background:#f0f0f0}
.pn{font-size:11px;font-weight:700;margin-top:6px}
.pp{font-size:12px;font-weight:900;margin-top:2px}
.hero{position:relative;line-height:0}
.hero img{width:100%;height:auto;display:block}
.hero .fallback{width:100%;height:220px;background:{{ $c['brand'] }}}
.hero .ov{position:absolute;inset:0;background:linear-gradient(to top,rgba(0,0,0,.72) 0%,rgba(0,0,0,.05) 60%);display:flex;flex-direction:column;justify-content:flex-end;padding:20px;line-height:1.2}
.hero h2{font-size:26px;font-weight:900;color:#fff}
.hero p{font-size:12px;color:rgba(255,255,255,.85);margin:6px 0 12px}
.hero button{align-self:flex-start;background:#fff;color:#111;border:none;padding:10px 18px;font-size:11px;font-weight:800;letter-spacing:.08em;border-radius:{{ $kit['cta_radius'] }};cursor:pointer}
.ad-bg{display:flex;align-items:center;justify-content:space-between;padding:18px 20px;gap:12px;background:{{ $c['brand'] }}}
.ad-text{min-width:0;flex:1}
.ad-text .at{font-size:9px;font-weight:700;color:#fff;opacity:.6;text-transform:uppercase;letter-spacing:.1em}
.ad-text .ab{font-size:18px;font-weight:900;color:#fff;line-height:1.1;word-break:keep-all;margin:4px 0}
.ad-text .ac{font-size:10px;color:rgba(255,255,255,.8)}
.ad-cta{flex-shrink:0;padding:10px 14px;white-space:nowrap;background:#fff;color:{{ $c['brand'] }};border:none;border-radius:{{ $kit['cta_radius'] }};font-size:11px;font-weight:800;cursor:pointer}
.nps-card{background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.nps-q{font-size:13px;font-weight:600;line-height:1.45}
.nps-nums{display:flex;justify-content:space-between;margin:16px 0 10px}
.nn{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;cursor:pointer;background:#f2f2f2}
.nn.active{background:{{ $c['brand'] }};color:#fff}
.nps-track{position:relative;height:6px;border-radius:6px;background:linear-gradient(90deg,#ff5b5b,#ffc107,#28c76f)}
.nps-handle{position:absolute;top:-4px;width:14px;height:14px;border-radius:50%;background:#fff;border:3px solid {{ $c['brand'] }};transition:left .18s}
.nps-lbls{display:flex;justify-content:space-between;font-size:10px;opacity:.6;margin-top:10px}
.show{position:relative;line-height:0}
.show img{width:100%;height:auto;display:block;filter:brightness(.75)}
.show .fallback{width:100%;height:200px;background:{{ $c['light'] }}}
.play{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:56px;height:56px;border-radius:50%;background:#fff;display:flex;align-items:center;justify-content:center}
.ig-h{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.ig-av{width:34px;height:34px;border-radius:50%;object-fit:cover;background:{{ $c['brand'] }}}
.ig-hn{font-size:12px;font-weight:800}
.ig-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:2px}
.ig-cell{aspect-ratio:1;overflow:hidden;background:#f0f0f0}
.ig-cell img{width:100%;height:100%;object-fit:cover;display:block}
.ig-more{aspect-ratio:1;background:{{ $c['brand'] }};color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800}
.exp-t{font-size:13px;font-weight:700;text-align:center;margin-bottom:16px}
.exp-row{display:flex;justify-content:space-between;gap:6px}
.exp-item{flex:1;text-align:center;cursor:pointer}
.exp-c{width:42px;height:42px;margin:0 auto;border-radius:50%;background:#f2f2f2;display:flex;align-items:center;justify-content:center;font-size:20px;transition:.15s}
.exp-item.sel .exp-c{background:{{ $c['brand'] }};transform:scale(1.1)}
.exp-l{font-size:9px;margin-top:6px;opacity:.6}
.exp-sub{width:100%;margin-top:18px;padding:12px;background:transparent;border:1.5px solid {{ $c['body_text'] }};color:{{ $c['body_text'] }};font-size:11px;font-weight:800;letter-spacing:.08em;border-radius:{{ $kit['cta_radius'] }};cursor:pointer}
.exp-sub:hover{background:{{ $c['body_text'] }};color:#fff}
.help-row{display:flex;align-items:center;gap:12px;margin-top:12px}
.help-ic{width:36px;height:36px;border-radius:8px;background:#f3f3f3;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.help-v{font-size:12px;font-weight:600}
.ft{background:{{ $c['brand'] }};padding:22px 16px;text-align:center}
.ft-l{color:#fff;font-size:10px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;margin-bottom:14px}
.ft-r{display:flex;justify-content:center;gap:8px}
.ft-i{width:34px;height:34px;border-radius:8px;background:rgba(255,255,255,.15);display:flex;align-items:center;justify-content:center}
.ft-i svg{fill:#fff}
.wordmark{font-size:18px;font-weight:900;letter-spacing:.18em;color:{{ $c['header_icon'] }}}
.hlogo img{height:24px;width:auto;display:block;margin:0 auto;{{ $kit['logo']['filter'] }}}
.hlogo .svgwrap{height:24px;display:flex;justify-content:center}
</style>
</head>
<body>
<div class="page">

  <div class="ann">{{ $kit['announcement'] }} &nbsp;&middot;&nbsp; <a href="{{ $kit['url'] }}">Shop Now</a></div>

  <div class="hdr">
    <div class="hbg"><span></span><span></span><span></span></div>
    <div class="hlogo">
      @if ($kit['logo']['mode'] === 'img')
        <img src="{{ $kit['logo']['src'] }}" alt="{{ $kit['brand_name'] }}"
             onerror="this.style.display='none'">
      @elseif ($kit['logo']['mode'] === 'svg')
        <div class="svgwrap">{!! $kit['logo']['svg'] !!}</div>
      @else
        <span class="wordmark">{{ strtoupper($kit['brand_name']) }}</span>
      @endif
    </div>
    <div class="hact"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="{{ $c['header_icon'] }}" stroke-width="1.7"><path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><path d="M3 6h18"/><path d="M16 10a4 4 0 0 1-8 0"/></svg></div>
  </div>

  <section><div class="card">
    <div class="g2">
      <div><div class="lbl">Order ID</div><div class="val">{{ $order['id'] }}</div></div>
      <div><div class="lbl">Order Placed On</div><div class="val">{{ $order['placed_on'] }}</div></div>
    </div>
    <div class="div"></div>
    <div class="otp"><span>Verify yourself to see complete order details and take action.</span><b>Verify &rsaquo;</b></div>
  </div></section>

  <section style="padding-top:0"><div class="card">
    <div class="g2">
      <div><div class="lbl">Estimated Delivery</div><div class="eta">{{ $order['eta'] }}</div></div>
      <div><div class="lbl">Your Order Is</div><div class="st">{{ $order['status'] }}</div></div>
    </div>
    <span class="status-badge">{{ $order['badge'] }}</span>
    <div class="div"></div>
    <div class="lbl" style="margin-bottom:8px">Tracking History</div>
    <div class="tl">
      <div class="tl-s">{{ $order['event'] }}</div>
      <div class="tl-d">{{ $order['event_desc'] }}</div>
      <div class="tl-t">{{ $order['event_time'] }}</div>
    </div>
  </div></section>

  {{-- build_kit ships only image-backed products (0, 2 or 4) — empty list hides the section --}}
  @if (count($kit['products']) >= 2)
  <section style="padding-bottom:0">
    <div class="sec-t">You May Also Like</div>
    <div class="pgrid">
      @foreach ($kit['products'] as $p)
        <div>
          <img class="pimg" src="{{ $p['image'] }}" alt="{{ $p['name'] }}"
               loading="lazy" onerror="this.style.display='none'">
          <div class="pn">{{ $p['name'] }}</div>
          <div class="pp">{{ $p['price'] }}</div>
        </div>
      @endforeach
    </div>
  </section>
  @endif

  <section style="padding:0">
    <div class="hero">
      @if ($kit['hero']['image'])
        <img src="{{ $kit['hero']['image'] }}" alt="" loading="lazy"
             onerror="this.style.display='none'">
      @else
        <div class="fallback"></div>
      @endif
      <div class="ov">
        <h2>{{ $kit['hero']['l1'] }}<br>{{ $kit['hero']['l2'] }}</h2>
        <p>{{ $kit['hero']['sub'] }}</p>
        <button>SHOP NOW</button>
      </div>
    </div>
  </section>

  <div class="ad-bg">
    <div class="ad-text">
      <div class="at">{{ $kit['ad']['eyebrow'] }}</div>
      <div class="ab">{{ $kit['ad']['l1'] }}<br>{{ $kit['ad']['l2'] }}</div>
      <div class="ac">{{ $kit['ad']['sub'] }}</div>
    </div>
    <button class="ad-cta">Shop Now</button>
  </div>

  <section><div class="nps-card">
    <div class="nps-q">How likely are you to recommend <strong>{{ $kit['brand_name'] }}</strong> to friends &amp; family?</div>
    <div class="nps-nums" id="npsNums"></div>
    <div class="nps-track"><div class="nps-handle" id="npsHandle"></div></div>
    <div class="nps-lbls"><span>&#128545; Not At All</span><span>Very Likely &#128525;</span></div>
  </div></section>

  @if ($kit['showcase_image'])
  <section style="padding:0">
    <div class="show">
      <img src="{{ $kit['showcase_image'] }}" alt="" loading="lazy"
           onerror="this.style.display='none'">
      <div class="play"><svg width="18" height="18" viewBox="0 0 24 24" fill="#111" style="margin-left:3px"><path d="M8 5v14l11-7z"/></svg></div>
    </div>
  </section>
  @endif

  @if (count($kit['instagram']['images']) >= 3)
  <section>
    <div class="ig-h">
      @if ($kit['logo']['src'])
        <img class="ig-av" src="{{ $kit['logo']['src'] }}" alt="">
      @else
        <div class="ig-av"></div>
      @endif
      <div class="ig-hn">&#64;{{ $kit['instagram']['handle'] }}</div>
    </div>
    <div class="ig-grid">
      @foreach (array_slice($kit['instagram']['images'], 0, 5) as $ig)
        <div class="ig-cell"><img src="{{ $ig }}" alt="" loading="lazy"></div>
      @endforeach
      <div class="ig-more">+MORE</div>
    </div>
  </section>
  @endif

  <section>
    <div class="exp-t">How was your delivery experience?</div>
    <div class="exp-row">
      <div class="exp-item" onclick="selExp(this)"><div class="exp-c">&#128544;</div><div class="exp-l">Terrible</div></div>
      <div class="exp-item" onclick="selExp(this)"><div class="exp-c">&#128533;</div><div class="exp-l">Bad</div></div>
      <div class="exp-item" onclick="selExp(this)"><div class="exp-c">&#128528;</div><div class="exp-l">Okay</div></div>
      <div class="exp-item" onclick="selExp(this)"><div class="exp-c">&#128522;</div><div class="exp-l">Good</div></div>
      <div class="exp-item" onclick="selExp(this)"><div class="exp-c">&#128513;</div><div class="exp-l">Excellent</div></div>
    </div>
    <button class="exp-sub">SUBMIT FEEDBACK</button>
  </section>

  <section style="padding-top:0">
    <div class="sec-t">Need Help?</div>
    <div class="help-row">
      <div class="help-ic"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#111" stroke-width="1.7"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 6-10 7L2 6"/></svg></div>
      <div class="help-v">support&#64;{{ $kit['domain'] }}</div>
    </div>
    <div class="help-row">
      <div class="help-ic"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#111" stroke-width="1.7"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2 4.2 2 2 0 0 1 4 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.2-1.2a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/></svg></div>
      <div class="help-v">+91-9876543210</div>
    </div>
  </section>

  <div class="ft">
    <div class="ft-l">Follow Us</div>
    <div class="ft-r">
      <div class="ft-i"><svg width="15" height="15" viewBox="0 0 24 24"><path d="M13 22v-9h3l.5-3.5H13V7.4c0-1 .3-1.7 1.7-1.7h1.9V2.6A25 25 0 0 0 14 2.4c-2.6 0-4.4 1.6-4.4 4.6v2.5H6.5V13h3.1v9z"/></svg></div>
      <div class="ft-i"><svg width="15" height="15" viewBox="0 0 24 24"><path d="M12 2.2c3.2 0 3.6 0 4.9.1 3.3.1 4.8 1.7 4.9 4.9.1 1.3.1 1.6.1 4.8s0 3.6-.1 4.9c-.1 3.2-1.6 4.8-4.9 4.9-1.3.1-1.6.1-4.9.1s-3.6 0-4.9-.1c-3.3-.2-4.8-1.7-4.9-4.9-.1-1.3-.1-1.6-.1-4.9s0-3.5.1-4.8C2.3 4 3.8 2.4 7.1 2.3c1.3-.1 1.7-.1 4.9-.1zm0 4.6a5.2 5.2 0 1 0 0 10.4 5.2 5.2 0 0 0 0-10.4zm0 8.6a3.4 3.4 0 1 1 0-6.8 3.4 3.4 0 0 1 0 6.8zm5.4-8.8a1.2 1.2 0 1 0 0-2.4 1.2 1.2 0 0 0 0 2.4z"/></svg></div>
      <div class="ft-i"><svg width="15" height="15" viewBox="0 0 24 24"><path d="M18.2 2H21l-6.4 7.3L22 22h-5.9l-4.6-6-5.3 6H3.4l6.9-7.8L2.5 2h6l4.2 5.5zm-1 18h1.6L7.9 3.7H6.2z"/></svg></div>
      <div class="ft-i"><svg width="15" height="15" viewBox="0 0 24 24"><path d="M23 12s0-3.8-.5-5.6a2.9 2.9 0 0 0-2-2C18.7 4 12 4 12 4s-6.7 0-8.5.5a2.9 2.9 0 0 0-2 2C1 8.2 1 12 1 12s0 3.8.5 5.6a2.9 2.9 0 0 0 2 2C5.3 20 12 20 12 20s6.7 0 8.5-.5a2.9 2.9 0 0 0 2-2C23 15.8 23 12 23 12zM9.8 15.4V8.6l5.9 3.4z"/></svg></div>
    </div>
  </div>

</div>
<script>
(function(){
  const w=document.getElementById('npsNums'),h=document.getElementById('npsHandle');
  let s=5;
  function r(){
    w.innerHTML='';
    for(let i=0;i<=10;i++){
      const d=document.createElement('div');
      d.className='nn'+(i===s?' active':'');
      d.textContent=i;
      const _i=i;
      d.onclick=function(){s=_i;r();h.style.left='calc('+(_i/10*100)+'% - 7px)'};
      w.appendChild(d);
    }
    h.style.left='calc('+(s/10*100)+'% - 7px)';
  }
  r();
})();
function selExp(el){
  document.querySelectorAll('.exp-item').forEach(e=>e.classList.remove('sel'));
  el.classList.add('sel');
}
</script>
</body>
</html>
