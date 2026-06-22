var Et=Object.defineProperty;var kt=Object.getOwnPropertyDescriptor;var v=(i,t,e,s)=>{for(var r=s>1?void 0:s?kt(t,e):t,o=i.length-1,n;o>=0;o--)(n=i[o])&&(r=(s?n(t,e,r):n(r))||r);return s&&r&&Et(t,e,r),r};var B=globalThis,D=B.ShadowRoot&&(B.ShadyCSS===void 0||B.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,K=Symbol(),nt=new WeakMap,z=class{constructor(t,e,s){if(this._$cssResult$=!0,s!==K)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o,e=this.t;if(D&&t===void 0){let s=e!==void 0&&e.length===1;s&&(t=nt.get(e)),t===void 0&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),s&&nt.set(e,t))}return t}toString(){return this.cssText}},at=i=>new z(typeof i=="string"?i:i+"",void 0,K),J=(i,...t)=>{let e=i.length===1?i[0]:t.reduce((s,r,o)=>s+(n=>{if(n._$cssResult$===!0)return n.cssText;if(typeof n=="number")return n;throw Error("Value passed to 'css' function must be a 'css' function result: "+n+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(r)+i[o+1],i[0]);return new z(e,i,K)},ct=(i,t)=>{if(D)i.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet);else for(let e of t){let s=document.createElement("style"),r=B.litNonce;r!==void 0&&s.setAttribute("nonce",r),s.textContent=e.cssText,i.appendChild(s)}},Y=D?i=>i:i=>i instanceof CSSStyleSheet?(t=>{let e="";for(let s of t.cssRules)e+=s.cssText;return at(e)})(i):i;var{is:Ct,defineProperty:zt,getOwnPropertyDescriptor:Pt,getOwnPropertyNames:Rt,getOwnPropertySymbols:Mt,getPrototypeOf:Ot}=Object,F=globalThis,lt=F.trustedTypes,Tt=lt?lt.emptyScript:"",Nt=F.reactiveElementPolyfillSupport,P=(i,t)=>i,R={toAttribute(i,t){switch(t){case Boolean:i=i?Tt:null;break;case Object:case Array:i=i==null?i:JSON.stringify(i)}return i},fromAttribute(i,t){let e=i;switch(t){case Boolean:e=i!==null;break;case Number:e=i===null?null:Number(i);break;case Object:case Array:try{e=JSON.parse(i)}catch{e=null}}return e}},q=(i,t)=>!Ct(i,t),ht={attribute:!0,type:String,converter:R,reflect:!1,useDefault:!1,hasChanged:q};Symbol.metadata??=Symbol("metadata"),F.litPropertyMetadata??=new WeakMap;var g=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=ht){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){let s=Symbol(),r=this.getPropertyDescriptor(t,s,e);r!==void 0&&zt(this.prototype,t,r)}}static getPropertyDescriptor(t,e,s){let{get:r,set:o}=Pt(this.prototype,t)??{get(){return this[e]},set(n){this[e]=n}};return{get:r,set(n){let l=r?.call(this);o?.call(this,n),this.requestUpdate(t,l,s)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??ht}static _$Ei(){if(this.hasOwnProperty(P("elementProperties")))return;let t=Ot(this);t.finalize(),t.l!==void 0&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(P("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(P("properties"))){let e=this.properties,s=[...Rt(e),...Mt(e)];for(let r of s)this.createProperty(r,e[r])}let t=this[Symbol.metadata];if(t!==null){let e=litPropertyMetadata.get(t);if(e!==void 0)for(let[s,r]of e)this.elementProperties.set(s,r)}this._$Eh=new Map;for(let[e,s]of this.elementProperties){let r=this._$Eu(e,s);r!==void 0&&this._$Eh.set(r,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){let e=[];if(Array.isArray(t)){let s=new Set(t.flat(1/0).reverse());for(let r of s)e.unshift(Y(r))}else t!==void 0&&e.push(Y(t));return e}static _$Eu(t,e){let s=e.attribute;return s===!1?void 0:typeof s=="string"?s:typeof t=="string"?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),this.renderRoot!==void 0&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){let t=new Map,e=this.constructor.elementProperties;for(let s of e.keys())this.hasOwnProperty(s)&&(t.set(s,this[s]),delete this[s]);t.size>0&&(this._$Ep=t)}createRenderRoot(){let t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return ct(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,s){this._$AK(t,s)}_$ET(t,e){let s=this.constructor.elementProperties.get(t),r=this.constructor._$Eu(t,s);if(r!==void 0&&s.reflect===!0){let o=(s.converter?.toAttribute!==void 0?s.converter:R).toAttribute(e,s.type);this._$Em=t,o==null?this.removeAttribute(r):this.setAttribute(r,o),this._$Em=null}}_$AK(t,e){let s=this.constructor,r=s._$Eh.get(t);if(r!==void 0&&this._$Em!==r){let o=s.getPropertyOptions(r),n=typeof o.converter=="function"?{fromAttribute:o.converter}:o.converter?.fromAttribute!==void 0?o.converter:R;this._$Em=r;let l=n.fromAttribute(e,o.type);this[r]=l??this._$Ej?.get(r)??l,this._$Em=null}}requestUpdate(t,e,s,r=!1,o){if(t!==void 0){let n=this.constructor;if(r===!1&&(o=this[t]),s??=n.getPropertyOptions(t),!((s.hasChanged??q)(o,e)||s.useDefault&&s.reflect&&o===this._$Ej?.get(t)&&!this.hasAttribute(n._$Eu(t,s))))return;this.C(t,e,s)}this.isUpdatePending===!1&&(this._$ES=this._$EP())}C(t,e,{useDefault:s,reflect:r,wrapped:o},n){s&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,n??e??this[t]),o!==!0||n!==void 0)||(this._$AL.has(t)||(this.hasUpdated||s||(e=void 0),this._$AL.set(t,e)),r===!0&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}let t=this.scheduleUpdate();return t!=null&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(let[r,o]of this._$Ep)this[r]=o;this._$Ep=void 0}let s=this.constructor.elementProperties;if(s.size>0)for(let[r,o]of s){let{wrapped:n}=o,l=this[r];n!==!0||this._$AL.has(r)||l===void 0||this.C(r,void 0,o,l)}}let t=!1,e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(s=>s.hostUpdate?.()),this.update(e)):this._$EM()}catch(s){throw t=!1,this._$EM(),s}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(e=>e.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(e=>this._$ET(e,this[e])),this._$EM()}updated(t){}firstUpdated(t){}};g.elementStyles=[],g.shadowRootOptions={mode:"open"},g[P("elementProperties")]=new Map,g[P("finalized")]=new Map,Nt?.({ReactiveElement:g}),(F.reactiveElementVersions??=[]).push("2.1.2");var st=globalThis,dt=i=>i,W=st.trustedTypes,pt=W?W.createPolicy("lit-html",{createHTML:i=>i}):void 0,gt="$lit$",b=`lit$${Math.random().toFixed(9).slice(2)}$`,$t="?"+b,Ut=`<${$t}>`,S=document,O=()=>S.createComment(""),T=i=>i===null||typeof i!="object"&&typeof i!="function",rt=Array.isArray,Ht=i=>rt(i)||typeof i?.[Symbol.iterator]=="function",Z=`[ 	
\f\r]`,M=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,ut=/-->/g,mt=/>/g,A=RegExp(`>|${Z}(?:([^\\s"'>=/]+)(${Z}*=${Z}*(?:[^ 	
\f\r"'\`<>=]|("|')|))|$)`,"g"),ft=/'/g,_t=/"/g,bt=/^(?:script|style|textarea|title)$/i,it=i=>(t,...e)=>({_$litType$:i,strings:t,values:e}),p=it(1),H=it(2),Yt=it(3),E=Symbol.for("lit-noChange"),u=Symbol.for("lit-nothing"),vt=new WeakMap,w=S.createTreeWalker(S,129);function yt(i,t){if(!rt(i)||!i.hasOwnProperty("raw"))throw Error("invalid template strings array");return pt!==void 0?pt.createHTML(t):t}var Lt=(i,t)=>{let e=i.length-1,s=[],r,o=t===2?"<svg>":t===3?"<math>":"",n=M;for(let l=0;l<e;l++){let a=i[l],h,d,c=-1,f=0;for(;f<a.length&&(n.lastIndex=f,d=n.exec(a),d!==null);)f=n.lastIndex,n===M?d[1]==="!--"?n=ut:d[1]!==void 0?n=mt:d[2]!==void 0?(bt.test(d[2])&&(r=RegExp("</"+d[2],"g")),n=A):d[3]!==void 0&&(n=A):n===A?d[0]===">"?(n=r??M,c=-1):d[1]===void 0?c=-2:(c=n.lastIndex-d[2].length,h=d[1],n=d[3]===void 0?A:d[3]==='"'?_t:ft):n===_t||n===ft?n=A:n===ut||n===mt?n=M:(n=A,r=void 0);let m=n===A&&i[l+1].startsWith("/>")?" ":"";o+=n===M?a+Ut:c>=0?(s.push(h),a.slice(0,c)+gt+a.slice(c)+b+m):a+b+(c===-2?l:m)}return[yt(i,o+(i[e]||"<?>")+(t===2?"</svg>":t===3?"</math>":"")),s]},N=class i{constructor({strings:t,_$litType$:e},s){let r;this.parts=[];let o=0,n=0,l=t.length-1,a=this.parts,[h,d]=Lt(t,e);if(this.el=i.createElement(h,s),w.currentNode=this.el.content,e===2||e===3){let c=this.el.content.firstChild;c.replaceWith(...c.childNodes)}for(;(r=w.nextNode())!==null&&a.length<l;){if(r.nodeType===1){if(r.hasAttributes())for(let c of r.getAttributeNames())if(c.endsWith(gt)){let f=d[n++],m=r.getAttribute(c).split(b),$=/([.?@])?(.*)/.exec(f);a.push({type:1,index:o,name:$[2],strings:m,ctor:$[1]==="."?Q:$[1]==="?"?X:$[1]==="@"?tt:C}),r.removeAttribute(c)}else c.startsWith(b)&&(a.push({type:6,index:o}),r.removeAttribute(c));if(bt.test(r.tagName)){let c=r.textContent.split(b),f=c.length-1;if(f>0){r.textContent=W?W.emptyScript:"";for(let m=0;m<f;m++)r.append(c[m],O()),w.nextNode(),a.push({type:2,index:++o});r.append(c[f],O())}}}else if(r.nodeType===8)if(r.data===$t)a.push({type:2,index:o});else{let c=-1;for(;(c=r.data.indexOf(b,c+1))!==-1;)a.push({type:7,index:o}),c+=b.length-1}o++}}static createElement(t,e){let s=S.createElement("template");return s.innerHTML=t,s}};function k(i,t,e=i,s){if(t===E)return t;let r=s!==void 0?e._$Co?.[s]:e._$Cl,o=T(t)?void 0:t._$litDirective$;return r?.constructor!==o&&(r?._$AO?.(!1),o===void 0?r=void 0:(r=new o(i),r._$AT(i,e,s)),s!==void 0?(e._$Co??=[])[s]=r:e._$Cl=r),r!==void 0&&(t=k(i,r._$AS(i,t.values),r,s)),t}var G=class{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){let{el:{content:e},parts:s}=this._$AD,r=(t?.creationScope??S).importNode(e,!0);w.currentNode=r;let o=w.nextNode(),n=0,l=0,a=s[0];for(;a!==void 0;){if(n===a.index){let h;a.type===2?h=new U(o,o.nextSibling,this,t):a.type===1?h=new a.ctor(o,a.name,a.strings,this,t):a.type===6&&(h=new et(o,this,t)),this._$AV.push(h),a=s[++l]}n!==a?.index&&(o=w.nextNode(),n++)}return w.currentNode=S,r}p(t){let e=0;for(let s of this._$AV)s!==void 0&&(s.strings!==void 0?(s._$AI(t,s,e),e+=s.strings.length-2):s._$AI(t[e])),e++}},U=class i{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,s,r){this.type=2,this._$AH=u,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=s,this.options=r,this._$Cv=r?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode,e=this._$AM;return e!==void 0&&t?.nodeType===11&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=k(this,t,e),T(t)?t===u||t==null||t===""?(this._$AH!==u&&this._$AR(),this._$AH=u):t!==this._$AH&&t!==E&&this._(t):t._$litType$!==void 0?this.$(t):t.nodeType!==void 0?this.T(t):Ht(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==u&&T(this._$AH)?this._$AA.nextSibling.data=t:this.T(S.createTextNode(t)),this._$AH=t}$(t){let{values:e,_$litType$:s}=t,r=typeof s=="number"?this._$AC(t):(s.el===void 0&&(s.el=N.createElement(yt(s.h,s.h[0]),this.options)),s);if(this._$AH?._$AD===r)this._$AH.p(e);else{let o=new G(r,this),n=o.u(this.options);o.p(e),this.T(n),this._$AH=o}}_$AC(t){let e=vt.get(t.strings);return e===void 0&&vt.set(t.strings,e=new N(t)),e}k(t){rt(this._$AH)||(this._$AH=[],this._$AR());let e=this._$AH,s,r=0;for(let o of t)r===e.length?e.push(s=new i(this.O(O()),this.O(O()),this,this.options)):s=e[r],s._$AI(o),r++;r<e.length&&(this._$AR(s&&s._$AB.nextSibling,r),e.length=r)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){let s=dt(t).nextSibling;dt(t).remove(),t=s}}setConnected(t){this._$AM===void 0&&(this._$Cv=t,this._$AP?.(t))}},C=class{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,s,r,o){this.type=1,this._$AH=u,this._$AN=void 0,this.element=t,this.name=e,this._$AM=r,this.options=o,s.length>2||s[0]!==""||s[1]!==""?(this._$AH=Array(s.length-1).fill(new String),this.strings=s):this._$AH=u}_$AI(t,e=this,s,r){let o=this.strings,n=!1;if(o===void 0)t=k(this,t,e,0),n=!T(t)||t!==this._$AH&&t!==E,n&&(this._$AH=t);else{let l=t,a,h;for(t=o[0],a=0;a<o.length-1;a++)h=k(this,l[s+a],e,a),h===E&&(h=this._$AH[a]),n||=!T(h)||h!==this._$AH[a],h===u?t=u:t!==u&&(t+=(h??"")+o[a+1]),this._$AH[a]=h}n&&!r&&this.j(t)}j(t){t===u?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}},Q=class extends C{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===u?void 0:t}},X=class extends C{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==u)}},tt=class extends C{constructor(t,e,s,r,o){super(t,e,s,r,o),this.type=5}_$AI(t,e=this){if((t=k(this,t,e,0)??u)===E)return;let s=this._$AH,r=t===u&&s!==u||t.capture!==s.capture||t.once!==s.once||t.passive!==s.passive,o=t!==u&&(s===u||r);r&&this.element.removeEventListener(this.name,this,s),o&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){typeof this._$AH=="function"?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}},et=class{constructor(t,e,s){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=s}get _$AU(){return this._$AM._$AU}_$AI(t){k(this,t)}};var jt=st.litHtmlPolyfillSupport;jt?.(N,U),(st.litHtmlVersions??=[]).push("3.3.3");var xt=(i,t,e)=>{let s=e?.renderBefore??t,r=s._$litPart$;if(r===void 0){let o=e?.renderBefore??null;s._$litPart$=r=new U(t.insertBefore(O(),o),o,void 0,e??{})}return r._$AI(i),r};var ot=globalThis,y=class extends g{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){let t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){let e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=xt(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return E}};y._$litElement$=!0,y.finalized=!0,ot.litElementHydrateSupport?.({LitElement:y});var Bt=ot.litElementPolyfillSupport;Bt?.({LitElement:y});(ot.litElementVersions??=[]).push("4.2.2");var At=i=>(t,e)=>{e!==void 0?e.addInitializer(()=>{customElements.define(i,t)}):customElements.define(i,t)};var Dt={attribute:!0,type:String,converter:R,reflect:!1,hasChanged:q},Ft=(i=Dt,t,e)=>{let{kind:s,metadata:r}=e,o=globalThis.litPropertyMetadata.get(r);if(o===void 0&&globalThis.litPropertyMetadata.set(r,o=new Map),s==="setter"&&((i=Object.create(i)).wrapped=!0),o.set(e.name,i),s==="accessor"){let{name:n}=e;return{set(l){let a=t.get.call(this);t.set.call(this,l),this.requestUpdate(n,a,i,!0,l)},init(l){return l!==void 0&&this.C(n,void 0,i,l),l}}}if(s==="setter"){let{name:n}=e;return function(l){let a=this[n];t.call(this,l),this.requestUpdate(n,a,i,!0,l)}}throw Error("Unsupported decorator location: "+s)};function L(i){return(t,e)=>typeof e=="object"?Ft(i,t,e):((s,r,o)=>{let n=r.hasOwnProperty(o);return r.constructor.createProperty(o,s),n?Object.getOwnPropertyDescriptor(r,o):void 0})(i,t,e)}function x(i){return L({...i,state:!0,attribute:!1})}var wt=["mon","tue","wed","thu","fri","sat","sun"],qt={mon:"Pon",tue:"Wt",wed:"\u015Ar",thu:"Czw",fri:"Pt",sat:"Sob",sun:"Nd"},St={"D+1":"#2ec4b6","D+2":"#7b6cf6","D+3":"#c98a3a"},_=class extends y{constructor(){super(...arguments);this.narrow=!1;this._tab="overview";this._plan=null;this._status=null;this._log=[];this._profiles=null;this._forecasts=null;this._error=null}connectedCallback(){super.connectedCallback(),this._refresh(),this._timer=window.setInterval(()=>this._refresh(),6e4)}disconnectedCallback(){this._timer&&window.clearInterval(this._timer),super.disconnectedCallback()}async _refresh(){if(this.hass)try{let[e,s,r,o]=await Promise.all([this.hass.callWS({type:"powerpilot/plan"}),this.hass.callWS({type:"powerpilot/status"}),this.hass.callWS({type:"powerpilot/log"}),this.hass.callWS({type:"powerpilot/profiles"})]);this._plan=e,this._status=s,this._log=r?.events??[],this._profiles=o,this._error=null}catch(e){this._error=e?.message??String(e)}}async _loadForecasts(){if(!(this._forecasts||!this.hass))try{this._forecasts=await this.hass.callWS({type:"powerpilot/forecasts"})}catch(e){this._error=e?.message??String(e)}}_selectTab(e){this._tab=e,e==="profiles"&&this._loadForecasts()}_openConfig(){window.location.assign("/config/integrations/integration/powerpilot")}render(){return p`
      <div class="header">
        <div class="title">PowerPilot</div>
        <div class="spacer"></div>
        <button class="cfg" @click=${this._openConfig}>⚙ Konfiguracja</button>
      </div>
      <div class="tabs">
        ${this._tabButton("overview","Przegl\u0105d")}
        ${this._tabButton("status","Status")}
        ${this._tabButton("profiles","Profile")}
        ${this._tabButton("logs","Logi")}
      </div>
      ${this._error?p`<div class="error">Błąd: ${this._error}</div>`:u}
      <div class="content">
        ${this._tab==="overview"?this._renderOverview():u}
        ${this._tab==="status"?this._renderStatus():u}
        ${this._tab==="profiles"?this._renderProfiles():u}
        ${this._tab==="logs"?this._renderLogs():u}
      </div>
    `}_tabButton(e,s){return p`<button
      class=${"tab"+(this._tab===e?" active":"")}
      @click=${()=>this._selectTab(e)}
    >
      ${s}
    </button>`}_renderOverview(){let e=this._plan;if(!e||!e.hours?.length)return p`<div class="card empty">Brak danych planu. Poczekaj na pierwsze przeliczenie.</div>`;let s=e.hours[0];return p`
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
    `}_stat(e,s){return p`<div class="stat"><span class="k">${e}</span><span class="v">${s}</span></div>`}_socChart(e){let s=e.hours.map(h=>h.battery_soc),r=e.forecast.map(h=>h.consumption_kwh),o=760,n=180,l=this._linePath(s,0,100,o,n),a=Math.max(.1,...r);return H`
      <svg viewBox="0 0 ${o} ${n}" class="chart">
        ${this._bars(r,0,a,o,n,"var(--error-color, #b5475d)")}
        <path d=${l} fill="none" stroke="var(--primary-color, #2ec4b6)" stroke-width="2" />
      </svg>`}_priceChart(e){let s=e.forecast.map(c=>c.buy_price??NaN),r=e.forecast.map(c=>c.sell_price??NaN),o=e.hours.map(c=>c.battery_energy_cost),n=[...s,...r,...o].filter(c=>!isNaN(c)),l=Math.min(0,...n),a=Math.max(.1,...n),h=760,d=180;return H`
      <svg viewBox="0 0 ${h} ${d}" class="chart">
        <path d=${this._linePath(s,l,a,h,d)} fill="none" stroke="var(--primary-color, #2ec4b6)" stroke-width="2" />
        <path d=${this._linePath(r,l,a,h,d)} fill="none" stroke="#7b6cf6" stroke-width="2" />
        <path d=${this._linePath(o,l,a,h,d)} fill="none" stroke="var(--secondary-text-color, #9e9e9e)" stroke-width="2" stroke-dasharray="4 3" />
      </svg>`}_linePath(e,s,r,o,n){let l=e.length;if(l<2)return"";let a=r-s||1,h=6,d=n-h*2,c="",f=!1;return e.forEach((m,$)=>{if(isNaN(m)){f=!1;return}let V=$/(l-1)*o,j=h+d-(m-s)/a*d;c+=`${f?"L":"M"}${V.toFixed(1)},${j.toFixed(1)} `,f=!0}),c.trim()}_bars(e,s,r,o,n,l){let a=e.length;if(!a)return u;let h=r-s||1,d=6,c=n-d*2,f=o/a*.7;return e.map((m,$)=>{let V=$/a*o,j=(m-s)/h*c;return H`<rect x=${V.toFixed(1)} y=${(d+c-j).toFixed(1)} width=${f.toFixed(1)} height=${Math.max(0,j).toFixed(1)} fill=${l} opacity="0.35" />`})}_renderStatus(){let e=this._status;return e?p`
      <div class="card">
        <div class="card-title">Co działa / czego brakuje</div>
        ${e.checks.map(s=>p`<div class="check">
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
        ${e.modules.map(s=>p`<div class="check">
            <span class=${"dot "+(s.error?"bad":"ok")}></span>${s.domain}
            ${s.error?p`<span class="muted">${s.error}</span>`:u}
          </div>`)}
      </div>
    `:p`<div class="card empty">Brak statusu.</div>`}_renderProfiles(){let e=this._profiles;return p`
      ${e?p`
            <div class="card">
              <div class="card-title">Profil cen — 7×24 (${e.price_days} dni)</div>
              ${this._heatmap(e.price,"PLN/kWh")}
            </div>
            <div class="card">
              <div class="card-title">Profil zużycia (bazowy) — 7×24 (${e.consumption_days} dni)</div>
              ${this._heatmap(e.consumption,"kWh")}
            </div>
          `:p`<div class="card empty">Ładowanie profili…</div>`}
      <div class="card">
        <div class="card-title">
          Prognozy D+1..D+3 ${this._forecasts?"\u2014 "+this._forecasts.date:""}
        </div>
        ${this._renderForecastOverlay()}
      </div>
    `}_heatmap(e,s){let r=[];if(wt.forEach(l=>(e[l]??[]).forEach(a=>{a!=null&&r.push(a)})),!r.length)return p`<div class="empty">Brak danych — profil jeszcze się uczy.</div>`;let o=Math.min(...r),n=Math.max(...r);return p`
      <div class="heatmap">
        <div class="hm-row hm-head">
          <div class="hm-label"></div>
          ${Array.from({length:24},(l,a)=>p`<div class="hm-h">${a}</div>`)}
        </div>
        ${wt.map(l=>p`
            <div class="hm-row">
              <div class="hm-label">${qt[l]}</div>
              ${(e[l]??[]).map(a=>{let h=a==null?"transparent":this._heatColor(a,o,n),d=a==null?"\u2014":`${a.toFixed(3)} ${s}`;return p`<div class="hm-cell" style=${"background:"+h} title=${d}></div>`})}
            </div>
          `)}
      </div>
      <div class="legend">
        <span>${o.toFixed(2)}</span>
        <div class="legend-bar"></div>
        <span>${n.toFixed(2)} ${s}</span>
      </div>
    `}_heatColor(e,s,r){return`hsl(${(1-(r>s?(e-s)/(r-s):.5))*160}, 70%, 45%)`}_renderForecastOverlay(){let e=this._forecasts;if(!e)return p`<div class="empty">Ładowanie prognoz…</div>`;let s=Object.keys(e.horizons||{});if(!s.length)return p`<div class="empty">Brak prognoz (wymaga źródła Pradcast z kluczem API).</div>`;let r=c=>{let f=new Array(24).fill(NaN);return c.forEach(m=>{m.buy!==null&&m.hour>=0&&m.hour<24&&(f[m.hour]=m.buy)}),f},o=s.map(c=>({h:c,vals:r(e.horizons[c])})),n=o.flatMap(c=>c.vals).filter(c=>!isNaN(c)),l=Math.min(0,...n),a=Math.max(.1,...n),h=760,d=180;return p`
      <svg viewBox="0 0 ${h} ${d}" class="chart">
        ${o.map(c=>H`<path d=${this._linePath(c.vals,l,a,h,d)} fill="none"
              stroke=${St[c.h]??"#888"} stroke-width="2" />`)}
      </svg>
      <div class="fc-legend">
        ${o.map(c=>p`<span class="fc-key">
            <span class="swatch" style=${"background:"+(St[c.h]??"#888")}></span>${c.h}
          </span>`)}
      </div>
    `}_renderLogs(){return this._log.length?p`<div class="card">
      <div class="card-title">Ostatnie przeliczenia</div>
      <table class="log">
        <thead>
          <tr><th>Czas</th><th>Akcja</th><th>SoC</th><th>EV</th><th>Horyzont</th><th>Błędy</th></tr>
        </thead>
        <tbody>
          ${this._log.map(e=>p`<tr>
              <td>${this._time(e.time)}</td>
              <td>${e.action??"\u2014"}</td>
              <td>${e.battery_soc??"\u2014"}</td>
              <td>${e.ev_charge?"tak":"\u2014"}</td>
              <td>${e.horizon_hours} h</td>
              <td class=${e.errors.length?"err":""}>${e.errors.join("; ")||"\u2014"}</td>
            </tr>`)}
        </tbody>
      </table>
    </div>`:p`<div class="card empty">Brak zdarzeń.</div>`}_time(e){try{return new Date(e).toLocaleString()}catch{return e}}};_.styles=J`
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
    .heatmap {
      display: flex;
      flex-direction: column;
      gap: 2px;
      overflow-x: auto;
    }
    .hm-row {
      display: flex;
      gap: 2px;
      align-items: center;
    }
    .hm-label {
      width: 34px;
      font-size: 12px;
      color: var(--secondary-text-color);
      flex: 0 0 auto;
    }
    .hm-h {
      width: 22px;
      text-align: center;
      font-size: 10px;
      color: var(--secondary-text-color);
      flex: 0 0 auto;
    }
    .hm-cell {
      width: 22px;
      height: 18px;
      border-radius: 2px;
      flex: 0 0 auto;
    }
    .legend {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      font-size: 12px;
      color: var(--secondary-text-color);
    }
    .legend-bar {
      flex: 1;
      max-width: 240px;
      height: 10px;
      border-radius: 5px;
      background: linear-gradient(
        90deg,
        hsl(160, 70%, 45%),
        hsl(80, 70%, 45%),
        hsl(0, 70%, 45%)
      );
    }
    .fc-legend {
      display: flex;
      gap: 16px;
      margin-top: 8px;
      font-size: 13px;
    }
    .fc-key {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .swatch {
      width: 12px;
      height: 12px;
      border-radius: 3px;
      display: inline-block;
    }
  `,v([L({attribute:!1})],_.prototype,"hass",2),v([L({attribute:!1})],_.prototype,"narrow",2),v([x()],_.prototype,"_tab",2),v([x()],_.prototype,"_plan",2),v([x()],_.prototype,"_status",2),v([x()],_.prototype,"_log",2),v([x()],_.prototype,"_profiles",2),v([x()],_.prototype,"_forecasts",2),v([x()],_.prototype,"_error",2),_=v([At("powerpilot-panel")],_);export{_ as PowerPilotPanel};
