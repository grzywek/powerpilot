var St=Object.defineProperty;var wt=Object.getOwnPropertyDescriptor;var $=(i,t,e,s)=>{for(var r=s>1?void 0:s?wt(t,e):t,o=i.length-1,n;o>=0;o--)(n=i[o])&&(r=(s?n(t,e,r):n(r))||r);return s&&r&&St(t,e,r),r};var L=globalThis,B=L.ShadowRoot&&(L.ShadyCSS===void 0||L.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,K=Symbol(),nt=new WeakMap,z=class{constructor(t,e,s){if(this._$cssResult$=!0,s!==K)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o,e=this.t;if(B&&t===void 0){let s=e!==void 0&&e.length===1;s&&(t=nt.get(e)),t===void 0&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),s&&nt.set(e,t))}return t}toString(){return this.cssText}},at=i=>new z(typeof i=="string"?i:i+"",void 0,K),J=(i,...t)=>{let e=i.length===1?i[0]:t.reduce((s,r,o)=>s+(n=>{if(n._$cssResult$===!0)return n.cssText;if(typeof n=="number")return n;throw Error("Value passed to 'css' function must be a 'css' function result: "+n+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(r)+i[o+1],i[0]);return new z(e,i,K)},ct=(i,t)=>{if(B)i.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet);else for(let e of t){let s=document.createElement("style"),r=L.litNonce;r!==void 0&&s.setAttribute("nonce",r),s.textContent=e.cssText,i.appendChild(s)}},Z=B?i=>i:i=>i instanceof CSSStyleSheet?(t=>{let e="";for(let s of t.cssRules)e+=s.cssText;return at(e)})(i):i;var{is:Et,defineProperty:Ct,getOwnPropertyDescriptor:kt,getOwnPropertyNames:zt,getOwnPropertySymbols:Tt,getPrototypeOf:Ut}=Object,D=globalThis,lt=D.trustedTypes,Ot=lt?lt.emptyScript:"",Rt=D.reactiveElementPolyfillSupport,T=(i,t)=>i,U={toAttribute(i,t){switch(t){case Boolean:i=i?Ot:null;break;case Object:case Array:i=i==null?i:JSON.stringify(i)}return i},fromAttribute(i,t){let e=i;switch(t){case Boolean:e=i!==null;break;case Number:e=i===null?null:Number(i);break;case Object:case Array:try{e=JSON.parse(i)}catch{e=null}}return e}},q=(i,t)=>!Et(i,t),ht={attribute:!0,type:String,converter:U,reflect:!1,useDefault:!1,hasChanged:q};Symbol.metadata??=Symbol("metadata"),D.litPropertyMetadata??=new WeakMap;var g=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=ht){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){let s=Symbol(),r=this.getPropertyDescriptor(t,s,e);r!==void 0&&Ct(this.prototype,t,r)}}static getPropertyDescriptor(t,e,s){let{get:r,set:o}=kt(this.prototype,t)??{get(){return this[e]},set(n){this[e]=n}};return{get:r,set(n){let c=r?.call(this);o?.call(this,n),this.requestUpdate(t,c,s)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??ht}static _$Ei(){if(this.hasOwnProperty(T("elementProperties")))return;let t=Ut(this);t.finalize(),t.l!==void 0&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(T("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(T("properties"))){let e=this.properties,s=[...zt(e),...Tt(e)];for(let r of s)this.createProperty(r,e[r])}let t=this[Symbol.metadata];if(t!==null){let e=litPropertyMetadata.get(t);if(e!==void 0)for(let[s,r]of e)this.elementProperties.set(s,r)}this._$Eh=new Map;for(let[e,s]of this.elementProperties){let r=this._$Eu(e,s);r!==void 0&&this._$Eh.set(r,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){let e=[];if(Array.isArray(t)){let s=new Set(t.flat(1/0).reverse());for(let r of s)e.unshift(Z(r))}else t!==void 0&&e.push(Z(t));return e}static _$Eu(t,e){let s=e.attribute;return s===!1?void 0:typeof s=="string"?s:typeof t=="string"?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),this.renderRoot!==void 0&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){let t=new Map,e=this.constructor.elementProperties;for(let s of e.keys())this.hasOwnProperty(s)&&(t.set(s,this[s]),delete this[s]);t.size>0&&(this._$Ep=t)}createRenderRoot(){let t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return ct(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,s){this._$AK(t,s)}_$ET(t,e){let s=this.constructor.elementProperties.get(t),r=this.constructor._$Eu(t,s);if(r!==void 0&&s.reflect===!0){let o=(s.converter?.toAttribute!==void 0?s.converter:U).toAttribute(e,s.type);this._$Em=t,o==null?this.removeAttribute(r):this.setAttribute(r,o),this._$Em=null}}_$AK(t,e){let s=this.constructor,r=s._$Eh.get(t);if(r!==void 0&&this._$Em!==r){let o=s.getPropertyOptions(r),n=typeof o.converter=="function"?{fromAttribute:o.converter}:o.converter?.fromAttribute!==void 0?o.converter:U;this._$Em=r;let c=n.fromAttribute(e,o.type);this[r]=c??this._$Ej?.get(r)??c,this._$Em=null}}requestUpdate(t,e,s,r=!1,o){if(t!==void 0){let n=this.constructor;if(r===!1&&(o=this[t]),s??=n.getPropertyOptions(t),!((s.hasChanged??q)(o,e)||s.useDefault&&s.reflect&&o===this._$Ej?.get(t)&&!this.hasAttribute(n._$Eu(t,s))))return;this.C(t,e,s)}this.isUpdatePending===!1&&(this._$ES=this._$EP())}C(t,e,{useDefault:s,reflect:r,wrapped:o},n){s&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,n??e??this[t]),o!==!0||n!==void 0)||(this._$AL.has(t)||(this.hasUpdated||s||(e=void 0),this._$AL.set(t,e)),r===!0&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}let t=this.scheduleUpdate();return t!=null&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(let[r,o]of this._$Ep)this[r]=o;this._$Ep=void 0}let s=this.constructor.elementProperties;if(s.size>0)for(let[r,o]of s){let{wrapped:n}=o,c=this[r];n!==!0||this._$AL.has(r)||c===void 0||this.C(r,void 0,o,c)}}let t=!1,e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(s=>s.hostUpdate?.()),this.update(e)):this._$EM()}catch(s){throw t=!1,this._$EM(),s}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(e=>e.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(e=>this._$ET(e,this[e])),this._$EM()}updated(t){}firstUpdated(t){}};g.elementStyles=[],g.shadowRootOptions={mode:"open"},g[T("elementProperties")]=new Map,g[T("finalized")]=new Map,Rt?.({ReactiveElement:g}),(D.reactiveElementVersions??=[]).push("2.1.2");var st=globalThis,dt=i=>i,I=st.trustedTypes,pt=I?I.createPolicy("lit-html",{createHTML:i=>i}):void 0,gt="$lit$",b=`lit$${Math.random().toFixed(9).slice(2)}$`,vt="?"+b,Pt=`<${vt}>`,S=document,R=()=>S.createComment(""),P=i=>i===null||typeof i!="object"&&typeof i!="function",rt=Array.isArray,Nt=i=>rt(i)||typeof i?.[Symbol.iterator]=="function",G=`[ 	
\f\r]`,O=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,ut=/-->/g,_t=/>/g,x=RegExp(`>|${G}(?:([^\\s"'>=/]+)(${G}*=${G}*(?:[^ 	
\f\r"'\`<>=]|("|')|))|$)`,"g"),mt=/'/g,ft=/"/g,bt=/^(?:script|style|textarea|title)$/i,it=i=>(t,...e)=>({_$litType$:i,strings:t,values:e}),_=it(1),F=it(2),Wt=it(3),w=Symbol.for("lit-noChange"),p=Symbol.for("lit-nothing"),$t=new WeakMap,A=S.createTreeWalker(S,129);function yt(i,t){if(!rt(i)||!i.hasOwnProperty("raw"))throw Error("invalid template strings array");return pt!==void 0?pt.createHTML(t):t}var Mt=(i,t)=>{let e=i.length-1,s=[],r,o=t===2?"<svg>":t===3?"<math>":"",n=O;for(let c=0;c<e;c++){let a=i[c],h,d,l=-1,u=0;for(;u<a.length&&(n.lastIndex=u,d=n.exec(a),d!==null);)u=n.lastIndex,n===O?d[1]==="!--"?n=ut:d[1]!==void 0?n=_t:d[2]!==void 0?(bt.test(d[2])&&(r=RegExp("</"+d[2],"g")),n=x):d[3]!==void 0&&(n=x):n===x?d[0]===">"?(n=r??O,l=-1):d[1]===void 0?l=-2:(l=n.lastIndex-d[2].length,h=d[1],n=d[3]===void 0?x:d[3]==='"'?ft:mt):n===ft||n===mt?n=x:n===ut||n===_t?n=O:(n=x,r=void 0);let m=n===x&&i[c+1].startsWith("/>")?" ":"";o+=n===O?a+Pt:l>=0?(s.push(h),a.slice(0,l)+gt+a.slice(l)+b+m):a+b+(l===-2?c:m)}return[yt(i,o+(i[e]||"<?>")+(t===2?"</svg>":t===3?"</math>":"")),s]},N=class i{constructor({strings:t,_$litType$:e},s){let r;this.parts=[];let o=0,n=0,c=t.length-1,a=this.parts,[h,d]=Mt(t,e);if(this.el=i.createElement(h,s),A.currentNode=this.el.content,e===2||e===3){let l=this.el.content.firstChild;l.replaceWith(...l.childNodes)}for(;(r=A.nextNode())!==null&&a.length<c;){if(r.nodeType===1){if(r.hasAttributes())for(let l of r.getAttributeNames())if(l.endsWith(gt)){let u=d[n++],m=r.getAttribute(l).split(b),v=/([.?@])?(.*)/.exec(u);a.push({type:1,index:o,name:v[2],strings:m,ctor:v[1]==="."?X:v[1]==="?"?Y:v[1]==="@"?tt:C}),r.removeAttribute(l)}else l.startsWith(b)&&(a.push({type:6,index:o}),r.removeAttribute(l));if(bt.test(r.tagName)){let l=r.textContent.split(b),u=l.length-1;if(u>0){r.textContent=I?I.emptyScript:"";for(let m=0;m<u;m++)r.append(l[m],R()),A.nextNode(),a.push({type:2,index:++o});r.append(l[u],R())}}}else if(r.nodeType===8)if(r.data===vt)a.push({type:2,index:o});else{let l=-1;for(;(l=r.data.indexOf(b,l+1))!==-1;)a.push({type:7,index:o}),l+=b.length-1}o++}}static createElement(t,e){let s=S.createElement("template");return s.innerHTML=t,s}};function E(i,t,e=i,s){if(t===w)return t;let r=s!==void 0?e._$Co?.[s]:e._$Cl,o=P(t)?void 0:t._$litDirective$;return r?.constructor!==o&&(r?._$AO?.(!1),o===void 0?r=void 0:(r=new o(i),r._$AT(i,e,s)),s!==void 0?(e._$Co??=[])[s]=r:e._$Cl=r),r!==void 0&&(t=E(i,r._$AS(i,t.values),r,s)),t}var Q=class{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){let{el:{content:e},parts:s}=this._$AD,r=(t?.creationScope??S).importNode(e,!0);A.currentNode=r;let o=A.nextNode(),n=0,c=0,a=s[0];for(;a!==void 0;){if(n===a.index){let h;a.type===2?h=new M(o,o.nextSibling,this,t):a.type===1?h=new a.ctor(o,a.name,a.strings,this,t):a.type===6&&(h=new et(o,this,t)),this._$AV.push(h),a=s[++c]}n!==a?.index&&(o=A.nextNode(),n++)}return A.currentNode=S,r}p(t){let e=0;for(let s of this._$AV)s!==void 0&&(s.strings!==void 0?(s._$AI(t,s,e),e+=s.strings.length-2):s._$AI(t[e])),e++}},M=class i{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,s,r){this.type=2,this._$AH=p,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=s,this.options=r,this._$Cv=r?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode,e=this._$AM;return e!==void 0&&t?.nodeType===11&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=E(this,t,e),P(t)?t===p||t==null||t===""?(this._$AH!==p&&this._$AR(),this._$AH=p):t!==this._$AH&&t!==w&&this._(t):t._$litType$!==void 0?this.$(t):t.nodeType!==void 0?this.T(t):Nt(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==p&&P(this._$AH)?this._$AA.nextSibling.data=t:this.T(S.createTextNode(t)),this._$AH=t}$(t){let{values:e,_$litType$:s}=t,r=typeof s=="number"?this._$AC(t):(s.el===void 0&&(s.el=N.createElement(yt(s.h,s.h[0]),this.options)),s);if(this._$AH?._$AD===r)this._$AH.p(e);else{let o=new Q(r,this),n=o.u(this.options);o.p(e),this.T(n),this._$AH=o}}_$AC(t){let e=$t.get(t.strings);return e===void 0&&$t.set(t.strings,e=new N(t)),e}k(t){rt(this._$AH)||(this._$AH=[],this._$AR());let e=this._$AH,s,r=0;for(let o of t)r===e.length?e.push(s=new i(this.O(R()),this.O(R()),this,this.options)):s=e[r],s._$AI(o),r++;r<e.length&&(this._$AR(s&&s._$AB.nextSibling,r),e.length=r)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){let s=dt(t).nextSibling;dt(t).remove(),t=s}}setConnected(t){this._$AM===void 0&&(this._$Cv=t,this._$AP?.(t))}},C=class{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,s,r,o){this.type=1,this._$AH=p,this._$AN=void 0,this.element=t,this.name=e,this._$AM=r,this.options=o,s.length>2||s[0]!==""||s[1]!==""?(this._$AH=Array(s.length-1).fill(new String),this.strings=s):this._$AH=p}_$AI(t,e=this,s,r){let o=this.strings,n=!1;if(o===void 0)t=E(this,t,e,0),n=!P(t)||t!==this._$AH&&t!==w,n&&(this._$AH=t);else{let c=t,a,h;for(t=o[0],a=0;a<o.length-1;a++)h=E(this,c[s+a],e,a),h===w&&(h=this._$AH[a]),n||=!P(h)||h!==this._$AH[a],h===p?t=p:t!==p&&(t+=(h??"")+o[a+1]),this._$AH[a]=h}n&&!r&&this.j(t)}j(t){t===p?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}},X=class extends C{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===p?void 0:t}},Y=class extends C{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==p)}},tt=class extends C{constructor(t,e,s,r,o){super(t,e,s,r,o),this.type=5}_$AI(t,e=this){if((t=E(this,t,e,0)??p)===w)return;let s=this._$AH,r=t===p&&s!==p||t.capture!==s.capture||t.once!==s.once||t.passive!==s.passive,o=t!==p&&(s===p||r);r&&this.element.removeEventListener(this.name,this,s),o&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){typeof this._$AH=="function"?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}},et=class{constructor(t,e,s){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=s}get _$AU(){return this._$AM._$AU}_$AI(t){E(this,t)}};var Ht=st.litHtmlPolyfillSupport;Ht?.(N,M),(st.litHtmlVersions??=[]).push("3.3.3");var xt=(i,t,e)=>{let s=e?.renderBefore??t,r=s._$litPart$;if(r===void 0){let o=e?.renderBefore??null;s._$litPart$=r=new M(t.insertBefore(R(),o),o,void 0,e??{})}return r._$AI(i),r};var ot=globalThis,y=class extends g{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){let t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){let e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=xt(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return w}};y._$litElement$=!0,y.finalized=!0,ot.litElementHydrateSupport?.({LitElement:y});var jt=ot.litElementPolyfillSupport;jt?.({LitElement:y});(ot.litElementVersions??=[]).push("4.2.2");var At=i=>(t,e)=>{e!==void 0?e.addInitializer(()=>{customElements.define(i,t)}):customElements.define(i,t)};var Lt={attribute:!0,type:String,converter:U,reflect:!1,hasChanged:q},Bt=(i=Lt,t,e)=>{let{kind:s,metadata:r}=e,o=globalThis.litPropertyMetadata.get(r);if(o===void 0&&globalThis.litPropertyMetadata.set(r,o=new Map),s==="setter"&&((i=Object.create(i)).wrapped=!0),o.set(e.name,i),s==="accessor"){let{name:n}=e;return{set(c){let a=t.get.call(this);t.set.call(this,c),this.requestUpdate(n,a,i,!0,c)},init(c){return c!==void 0&&this.C(n,void 0,i,c),c}}}if(s==="setter"){let{name:n}=e;return function(c){let a=this[n];t.call(this,c),this.requestUpdate(n,a,i,!0,c)}}throw Error("Unsupported decorator location: "+s)};function H(i){return(t,e)=>typeof e=="object"?Bt(i,t,e):((s,r,o)=>{let n=r.hasOwnProperty(o);return r.constructor.createProperty(o,s),n?Object.getOwnPropertyDescriptor(r,o):void 0})(i,t,e)}function k(i){return H({...i,state:!0,attribute:!1})}var f=class extends y{constructor(){super(...arguments);this.narrow=!1;this._tab="overview";this._plan=null;this._status=null;this._log=[];this._error=null}connectedCallback(){super.connectedCallback(),this._refresh(),this._timer=window.setInterval(()=>this._refresh(),6e4)}disconnectedCallback(){this._timer&&window.clearInterval(this._timer),super.disconnectedCallback()}async _refresh(){if(this.hass)try{let[e,s,r]=await Promise.all([this.hass.callWS({type:"powerpilot/plan"}),this.hass.callWS({type:"powerpilot/status"}),this.hass.callWS({type:"powerpilot/log"})]);this._plan=e,this._status=s,this._log=r?.events??[],this._error=null}catch(e){this._error=e?.message??String(e)}}_openConfig(){window.location.assign("/config/integrations/integration/powerpilot")}render(){return _`
      <div class="header">
        <div class="title">PowerPilot</div>
        <div class="spacer"></div>
        <button class="cfg" @click=${this._openConfig}>⚙ Konfiguracja</button>
      </div>
      <div class="tabs">
        ${this._tabButton("overview","Przegl\u0105d")}
        ${this._tabButton("status","Status")}
        ${this._tabButton("logs","Logi")}
      </div>
      ${this._error?_`<div class="error">Błąd: ${this._error}</div>`:p}
      <div class="content">
        ${this._tab==="overview"?this._renderOverview():p}
        ${this._tab==="status"?this._renderStatus():p}
        ${this._tab==="logs"?this._renderLogs():p}
      </div>
    `}_tabButton(e,s){return _`<button
      class=${"tab"+(this._tab===e?" active":"")}
      @click=${()=>this._tab=e}
    >
      ${s}
    </button>`}_renderOverview(){let e=this._plan;if(!e||!e.hours?.length)return _`<div class="card empty">Brak danych planu. Poczekaj na pierwsze przeliczenie.</div>`;let s=e.hours[0];return _`
      <div class="card">
        <div class="stat-row">
          ${this._stat("Tryb falownika",s.inverter_mode)}
          ${this._stat("Moc",s.charge_power)}
          ${this._stat("SoC",s.battery_soc.toFixed(0)+" %")}
          ${this._stat("Cena w baterii",s.battery_energy_cost.toFixed(3))}
          ${this._stat("Sie\u0107",s.grid_connected?"tak":"nie")}
          ${this._stat("EV",s.ev_charge?"\u0142aduje":"\u2014")}
          ${this._stat("Koszt horyzontu",e.total_cost.toFixed(2)+" PLN")}
        </div>
      </div>
      <div class="card">
        <div class="card-title">Bateria (SoC %) i zużycie</div>
        ${this._socChart(e)}
      </div>
      <div class="card">
        <div class="card-title">Ceny (PLN/kWh) — zakup, sprzedaż, cena w baterii</div>
        ${this._priceChart(e)}
      </div>
    `}_stat(e,s){return _`<div class="stat"><span class="k">${e}</span><span class="v">${s}</span></div>`}_socChart(e){let s=e.hours.map(h=>h.battery_soc),r=e.forecast.map(h=>h.consumption_kwh),o=760,n=180,c=this._linePath(s,0,100,o,n),a=Math.max(.1,...r);return F`
      <svg viewBox="0 0 ${o} ${n}" class="chart">
        ${this._bars(r,0,a,o,n,"var(--error-color, #b5475d)")}
        <path d=${c} fill="none" stroke="var(--primary-color, #2ec4b6)" stroke-width="2" />
      </svg>`}_priceChart(e){let s=e.forecast.map(l=>l.buy_price??NaN),r=e.forecast.map(l=>l.sell_price??NaN),o=e.hours.map(l=>l.battery_energy_cost),n=[...s,...r,...o].filter(l=>!isNaN(l)),c=Math.min(0,...n),a=Math.max(.1,...n),h=760,d=180;return F`
      <svg viewBox="0 0 ${h} ${d}" class="chart">
        <path d=${this._linePath(s,c,a,h,d)} fill="none" stroke="var(--primary-color, #2ec4b6)" stroke-width="2" />
        <path d=${this._linePath(r,c,a,h,d)} fill="none" stroke="#7b6cf6" stroke-width="2" />
        <path d=${this._linePath(o,c,a,h,d)} fill="none" stroke="var(--secondary-text-color, #9e9e9e)" stroke-width="2" stroke-dasharray="4 3" />
      </svg>`}_linePath(e,s,r,o,n){let c=e.length;if(c<2)return"";let a=r-s||1,h=6,d=n-h*2,l="",u=!1;return e.forEach((m,v)=>{if(isNaN(m)){u=!1;return}let W=v/(c-1)*o,j=h+d-(m-s)/a*d;l+=`${u?"L":"M"}${W.toFixed(1)},${j.toFixed(1)} `,u=!0}),l.trim()}_bars(e,s,r,o,n,c){let a=e.length;if(!a)return p;let h=r-s||1,d=6,l=n-d*2,u=o/a*.7;return e.map((m,v)=>{let W=v/a*o,j=(m-s)/h*l;return F`<rect x=${W.toFixed(1)} y=${(d+l-j).toFixed(1)} width=${u.toFixed(1)} height=${Math.max(0,j).toFixed(1)} fill=${c} opacity="0.35" />`})}_renderStatus(){let e=this._status;return e?_`
      <div class="card">
        <div class="card-title">Co działa / czego brakuje</div>
        ${e.checks.map(s=>_`<div class="check">
            <span class=${"dot "+(s.ok?"ok":"bad")}></span>${s.label}
            <span class="muted">${s.ok?"OK":"brak konfiguracji"}</span>
          </div>`)}
      </div>
      <div class="card">
        <div class="card-title">Uczenie</div>
        <div class="check">Profil cen: <b>${e.price_profile_days}</b> dni</div>
        <div class="check">Profil zużycia: <b>${e.consumption_days}</b> dni</div>
        <div class="check">
          Urządzenia rozdzielone:
          <b>${e.consumption_devices.length?e.consumption_devices.join(", "):"brak"}</b>
        </div>
        <div class="check">EV: <b>${e.ev_enabled?"w\u0142\u0105czone":"wy\u0142\u0105czone"}</b></div>
        <div class="check">Horyzont planu: <b>${e.horizon_hours}</b> h</div>
      </div>
      <div class="card">
        <div class="card-title">Moduły</div>
        ${e.modules.map(s=>_`<div class="check">
            <span class=${"dot "+(s.error?"bad":"ok")}></span>${s.domain}
            ${s.error?_`<span class="muted">${s.error}</span>`:p}
          </div>`)}
      </div>
    `:_`<div class="card empty">Brak statusu.</div>`}_renderLogs(){return this._log.length?_`<div class="card">
      <div class="card-title">Ostatnie przeliczenia</div>
      <table class="log">
        <thead>
          <tr><th>Czas</th><th>Akcja</th><th>SoC</th><th>EV</th><th>Horyzont</th><th>Błędy</th></tr>
        </thead>
        <tbody>
          ${this._log.map(e=>_`<tr>
              <td>${this._time(e.time)}</td>
              <td>${e.action??"\u2014"}</td>
              <td>${e.battery_soc??"\u2014"}</td>
              <td>${e.ev_charge?"tak":"\u2014"}</td>
              <td>${e.horizon_hours} h</td>
              <td class=${e.errors.length?"err":""}>${e.errors.join("; ")||"\u2014"}</td>
            </tr>`)}
        </tbody>
      </table>
    </div>`:_`<div class="card empty">Brak zdarzeń.</div>`}_time(e){try{return new Date(e).toLocaleString()}catch{return e}}};f.styles=J`
    :host {
      display: block;
      padding: 16px;
      color: var(--primary-text-color);
      background: var(--primary-background-color);
      min-height: 100vh;
      box-sizing: border-box;
    }
    .header {
      display: flex;
      align-items: center;
      margin-bottom: 12px;
    }
    .title {
      font-size: 22px;
      font-weight: 600;
    }
    .spacer {
      flex: 1;
    }
    .cfg {
      cursor: pointer;
      border: 1px solid var(--divider-color);
      background: var(--card-background-color);
      color: var(--primary-text-color);
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 14px;
    }
    .tabs {
      display: flex;
      gap: 8px;
      margin-bottom: 12px;
    }
    .tab {
      cursor: pointer;
      border: none;
      background: var(--card-background-color);
      color: var(--secondary-text-color);
      border-radius: 8px;
      padding: 8px 14px;
      font-size: 14px;
    }
    .tab.active {
      color: var(--text-primary-color, #fff);
      background: var(--primary-color);
    }
    .content {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .card {
      background: var(--card-background-color, #1c1c1c);
      border-radius: 12px;
      padding: 16px;
      box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0, 0, 0, 0.2));
    }
    .card-title {
      font-weight: 600;
      margin-bottom: 10px;
    }
    .empty {
      color: var(--secondary-text-color);
    }
    .error {
      color: var(--error-color, #d33);
      margin-bottom: 12px;
    }
    .chart {
      width: 100%;
      height: auto;
      display: block;
    }
    .stat-row {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
    }
    .stat {
      display: flex;
      flex-direction: column;
    }
    .stat .k {
      font-size: 12px;
      color: var(--secondary-text-color);
    }
    .stat .v {
      font-size: 18px;
      font-weight: 600;
    }
    .check {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 4px 0;
    }
    .muted {
      color: var(--secondary-text-color);
      font-size: 13px;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
    }
    .dot.ok {
      background: var(--success-color, #43a047);
    }
    .dot.bad {
      background: var(--error-color, #d33);
    }
    table.log {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    table.log th,
    table.log td {
      text-align: left;
      padding: 6px 8px;
      border-bottom: 1px solid var(--divider-color);
    }
    td.err {
      color: var(--error-color, #d33);
    }
  `,$([H({attribute:!1})],f.prototype,"hass",2),$([H({attribute:!1})],f.prototype,"narrow",2),$([k()],f.prototype,"_tab",2),$([k()],f.prototype,"_plan",2),$([k()],f.prototype,"_status",2),$([k()],f.prototype,"_log",2),$([k()],f.prototype,"_error",2),f=$([At("powerpilot-panel")],f);export{f as PowerPilotPanel};
