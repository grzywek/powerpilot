var Lt=Object.defineProperty;var Ft=Object.getOwnPropertyDescriptor;var x=(n,t,e,s)=>{for(var r=s>1?void 0:s?Ft(t,e):t,i=n.length-1,o;i>=0;i--)(o=n[i])&&(r=(s?o(t,e,r):o(r))||r);return s&&r&&Lt(t,e,r),r};var J=globalThis,Y=J.ShadowRoot&&(J.ShadyCSS===void 0||J.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,rt=Symbol(),ft=new WeakMap,F=class{constructor(t,e,s){if(this._$cssResult$=!0,s!==rt)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o,e=this.t;if(Y&&t===void 0){let s=e!==void 0&&e.length===1;s&&(t=ft.get(e)),t===void 0&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),s&&ft.set(e,t))}return t}toString(){return this.cssText}},gt=n=>new F(typeof n=="string"?n:n+"",void 0,rt),it=(n,...t)=>{let e=n.length===1?n[0]:t.reduce((s,r,i)=>s+(o=>{if(o._$cssResult$===!0)return o.cssText;if(typeof o=="number")return o;throw Error("Value passed to 'css' function must be a 'css' function result: "+o+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(r)+n[i+1],n[0]);return new F(e,n,rt)},yt=(n,t)=>{if(Y)n.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet);else for(let e of t){let s=document.createElement("style"),r=J.litNonce;r!==void 0&&s.setAttribute("nonce",r),s.textContent=e.cssText,n.appendChild(s)}},ot=Y?n=>n:n=>n instanceof CSSStyleSheet?(t=>{let e="";for(let s of t.cssRules)e+=s.cssText;return gt(e)})(n):n;var{is:Bt,defineProperty:Dt,getOwnPropertyDescriptor:jt,getOwnPropertyNames:Wt,getOwnPropertySymbols:qt,getPrototypeOf:It}=Object,G=globalThis,$t=G.trustedTypes,Vt=$t?$t.emptyScript:"",Kt=G.reactiveElementPolyfillSupport,B=(n,t)=>n,D={toAttribute(n,t){switch(t){case Boolean:n=n?Vt:null;break;case Object:case Array:n=n==null?n:JSON.stringify(n)}return n},fromAttribute(n,t){let e=n;switch(t){case Boolean:e=n!==null;break;case Number:e=n===null?null:Number(n);break;case Object:case Array:try{e=JSON.parse(n)}catch{e=null}}return e}},Q=(n,t)=>!Bt(n,t),vt={attribute:!0,type:String,converter:D,reflect:!1,useDefault:!1,hasChanged:Q};Symbol.metadata??=Symbol("metadata"),G.litPropertyMetadata??=new WeakMap;var A=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=vt){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){let s=Symbol(),r=this.getPropertyDescriptor(t,s,e);r!==void 0&&Dt(this.prototype,t,r)}}static getPropertyDescriptor(t,e,s){let{get:r,set:i}=jt(this.prototype,t)??{get(){return this[e]},set(o){this[e]=o}};return{get:r,set(o){let c=r?.call(this);i?.call(this,o),this.requestUpdate(t,c,s)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??vt}static _$Ei(){if(this.hasOwnProperty(B("elementProperties")))return;let t=It(this);t.finalize(),t.l!==void 0&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(B("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(B("properties"))){let e=this.properties,s=[...Wt(e),...qt(e)];for(let r of s)this.createProperty(r,e[r])}let t=this[Symbol.metadata];if(t!==null){let e=litPropertyMetadata.get(t);if(e!==void 0)for(let[s,r]of e)this.elementProperties.set(s,r)}this._$Eh=new Map;for(let[e,s]of this.elementProperties){let r=this._$Eu(e,s);r!==void 0&&this._$Eh.set(r,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){let e=[];if(Array.isArray(t)){let s=new Set(t.flat(1/0).reverse());for(let r of s)e.unshift(ot(r))}else t!==void 0&&e.push(ot(t));return e}static _$Eu(t,e){let s=e.attribute;return s===!1?void 0:typeof s=="string"?s:typeof t=="string"?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),this.renderRoot!==void 0&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){let t=new Map,e=this.constructor.elementProperties;for(let s of e.keys())this.hasOwnProperty(s)&&(t.set(s,this[s]),delete this[s]);t.size>0&&(this._$Ep=t)}createRenderRoot(){let t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return yt(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,s){this._$AK(t,s)}_$ET(t,e){let s=this.constructor.elementProperties.get(t),r=this.constructor._$Eu(t,s);if(r!==void 0&&s.reflect===!0){let i=(s.converter?.toAttribute!==void 0?s.converter:D).toAttribute(e,s.type);this._$Em=t,i==null?this.removeAttribute(r):this.setAttribute(r,i),this._$Em=null}}_$AK(t,e){let s=this.constructor,r=s._$Eh.get(t);if(r!==void 0&&this._$Em!==r){let i=s.getPropertyOptions(r),o=typeof i.converter=="function"?{fromAttribute:i.converter}:i.converter?.fromAttribute!==void 0?i.converter:D;this._$Em=r;let c=o.fromAttribute(e,i.type);this[r]=c??this._$Ej?.get(r)??c,this._$Em=null}}requestUpdate(t,e,s,r=!1,i){if(t!==void 0){let o=this.constructor;if(r===!1&&(i=this[t]),s??=o.getPropertyOptions(t),!((s.hasChanged??Q)(i,e)||s.useDefault&&s.reflect&&i===this._$Ej?.get(t)&&!this.hasAttribute(o._$Eu(t,s))))return;this.C(t,e,s)}this.isUpdatePending===!1&&(this._$ES=this._$EP())}C(t,e,{useDefault:s,reflect:r,wrapped:i},o){s&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,o??e??this[t]),i!==!0||o!==void 0)||(this._$AL.has(t)||(this.hasUpdated||s||(e=void 0),this._$AL.set(t,e)),r===!0&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}let t=this.scheduleUpdate();return t!=null&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(let[r,i]of this._$Ep)this[r]=i;this._$Ep=void 0}let s=this.constructor.elementProperties;if(s.size>0)for(let[r,i]of s){let{wrapped:o}=i,c=this[r];o!==!0||this._$AL.has(r)||c===void 0||this.C(r,void 0,i,c)}}let t=!1,e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(s=>s.hostUpdate?.()),this.update(e)):this._$EM()}catch(s){throw t=!1,this._$EM(),s}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(e=>e.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(e=>this._$ET(e,this[e])),this._$EM()}updated(t){}firstUpdated(t){}};A.elementStyles=[],A.shadowRootOptions={mode:"open"},A[B("elementProperties")]=new Map,A[B("finalized")]=new Map,Kt?.({ReactiveElement:A}),(G.reactiveElementVersions??=[]).push("2.1.2");var pt=globalThis,bt=n=>n,X=pt.trustedTypes,xt=X?X.createPolicy("lit-html",{createHTML:n=>n}):void 0,Ct="$lit$",E=`lit$${Math.random().toFixed(9).slice(2)}$`,zt="?"+E,Zt=`<${zt}>`,R=document,W=()=>R.createComment(""),q=n=>n===null||typeof n!="object"&&typeof n!="function",ut=Array.isArray,Jt=n=>ut(n)||typeof n?.[Symbol.iterator]=="function",nt=`[ 	
\f\r]`,j=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,wt=/-->/g,At=/>/g,z=RegExp(`>|${nt}(?:([^\\s"'>=/]+)(${nt}*=${nt}*(?:[^ 	
\f\r"'\`<>=]|("|')|))|$)`,"g"),kt=/'/g,St=/"/g,Mt=/^(?:script|style|textarea|title)$/i,mt=n=>(t,...e)=>({_$litType$:n,strings:t,values:e}),p=mt(1),y=mt(2),ce=mt(3),T=Symbol.for("lit-noChange"),m=Symbol.for("lit-nothing"),Et=new WeakMap,M=R.createTreeWalker(R,129);function Rt(n,t){if(!ut(n)||!n.hasOwnProperty("raw"))throw Error("invalid template strings array");return xt!==void 0?xt.createHTML(t):t}var Yt=(n,t)=>{let e=n.length-1,s=[],r,i=t===2?"<svg>":t===3?"<math>":"",o=j;for(let c=0;c<e;c++){let a=n[c],d,u,l=-1,g=0;for(;g<a.length&&(o.lastIndex=g,u=o.exec(a),u!==null);)g=o.lastIndex,o===j?u[1]==="!--"?o=wt:u[1]!==void 0?o=At:u[2]!==void 0?(Mt.test(u[2])&&(r=RegExp("</"+u[2],"g")),o=z):u[3]!==void 0&&(o=z):o===z?u[0]===">"?(o=r??j,l=-1):u[1]===void 0?l=-2:(l=o.lastIndex-u[2].length,d=u[1],o=u[3]===void 0?z:u[3]==='"'?St:kt):o===St||o===kt?o=z:o===wt||o===At?o=j:(o=z,r=void 0);let f=o===z&&n[c+1].startsWith("/>")?" ":"";i+=o===j?a+Zt:l>=0?(s.push(d),a.slice(0,l)+Ct+a.slice(l)+E+f):a+E+(l===-2?c:f)}return[Rt(n,i+(n[e]||"<?>")+(t===2?"</svg>":t===3?"</math>":"")),s]},I=class n{constructor({strings:t,_$litType$:e},s){let r;this.parts=[];let i=0,o=0,c=t.length-1,a=this.parts,[d,u]=Yt(t,e);if(this.el=n.createElement(d,s),M.currentNode=this.el.content,e===2||e===3){let l=this.el.content.firstChild;l.replaceWith(...l.childNodes)}for(;(r=M.nextNode())!==null&&a.length<c;){if(r.nodeType===1){if(r.hasAttributes())for(let l of r.getAttributeNames())if(l.endsWith(Ct)){let g=u[o++],f=r.getAttribute(l).split(E),b=/([.?@])?(.*)/.exec(g);a.push({type:1,index:i,name:b[2],strings:f,ctor:b[1]==="."?ct:b[1]==="?"?lt:b[1]==="@"?ht:U}),r.removeAttribute(l)}else l.startsWith(E)&&(a.push({type:6,index:i}),r.removeAttribute(l));if(Mt.test(r.tagName)){let l=r.textContent.split(E),g=l.length-1;if(g>0){r.textContent=X?X.emptyScript:"";for(let f=0;f<g;f++)r.append(l[f],W()),M.nextNode(),a.push({type:2,index:++i});r.append(l[g],W())}}}else if(r.nodeType===8)if(r.data===zt)a.push({type:2,index:i});else{let l=-1;for(;(l=r.data.indexOf(E,l+1))!==-1;)a.push({type:7,index:i}),l+=E.length-1}i++}}static createElement(t,e){let s=R.createElement("template");return s.innerHTML=t,s}};function H(n,t,e=n,s){if(t===T)return t;let r=s!==void 0?e._$Co?.[s]:e._$Cl,i=q(t)?void 0:t._$litDirective$;return r?.constructor!==i&&(r?._$AO?.(!1),i===void 0?r=void 0:(r=new i(n),r._$AT(n,e,s)),s!==void 0?(e._$Co??=[])[s]=r:e._$Cl=r),r!==void 0&&(t=H(n,r._$AS(n,t.values),r,s)),t}var at=class{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){let{el:{content:e},parts:s}=this._$AD,r=(t?.creationScope??R).importNode(e,!0);M.currentNode=r;let i=M.nextNode(),o=0,c=0,a=s[0];for(;a!==void 0;){if(o===a.index){let d;a.type===2?d=new V(i,i.nextSibling,this,t):a.type===1?d=new a.ctor(i,a.name,a.strings,this,t):a.type===6&&(d=new dt(i,this,t)),this._$AV.push(d),a=s[++c]}o!==a?.index&&(i=M.nextNode(),o++)}return M.currentNode=R,r}p(t){let e=0;for(let s of this._$AV)s!==void 0&&(s.strings!==void 0?(s._$AI(t,s,e),e+=s.strings.length-2):s._$AI(t[e])),e++}},V=class n{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,s,r){this.type=2,this._$AH=m,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=s,this.options=r,this._$Cv=r?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode,e=this._$AM;return e!==void 0&&t?.nodeType===11&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=H(this,t,e),q(t)?t===m||t==null||t===""?(this._$AH!==m&&this._$AR(),this._$AH=m):t!==this._$AH&&t!==T&&this._(t):t._$litType$!==void 0?this.$(t):t.nodeType!==void 0?this.T(t):Jt(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==m&&q(this._$AH)?this._$AA.nextSibling.data=t:this.T(R.createTextNode(t)),this._$AH=t}$(t){let{values:e,_$litType$:s}=t,r=typeof s=="number"?this._$AC(t):(s.el===void 0&&(s.el=I.createElement(Rt(s.h,s.h[0]),this.options)),s);if(this._$AH?._$AD===r)this._$AH.p(e);else{let i=new at(r,this),o=i.u(this.options);i.p(e),this.T(o),this._$AH=i}}_$AC(t){let e=Et.get(t.strings);return e===void 0&&Et.set(t.strings,e=new I(t)),e}k(t){ut(this._$AH)||(this._$AH=[],this._$AR());let e=this._$AH,s,r=0;for(let i of t)r===e.length?e.push(s=new n(this.O(W()),this.O(W()),this,this.options)):s=e[r],s._$AI(i),r++;r<e.length&&(this._$AR(s&&s._$AB.nextSibling,r),e.length=r)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){let s=bt(t).nextSibling;bt(t).remove(),t=s}}setConnected(t){this._$AM===void 0&&(this._$Cv=t,this._$AP?.(t))}},U=class{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,s,r,i){this.type=1,this._$AH=m,this._$AN=void 0,this.element=t,this.name=e,this._$AM=r,this.options=i,s.length>2||s[0]!==""||s[1]!==""?(this._$AH=Array(s.length-1).fill(new String),this.strings=s):this._$AH=m}_$AI(t,e=this,s,r){let i=this.strings,o=!1;if(i===void 0)t=H(this,t,e,0),o=!q(t)||t!==this._$AH&&t!==T,o&&(this._$AH=t);else{let c=t,a,d;for(t=i[0],a=0;a<i.length-1;a++)d=H(this,c[s+a],e,a),d===T&&(d=this._$AH[a]),o||=!q(d)||d!==this._$AH[a],d===m?t=m:t!==m&&(t+=(d??"")+i[a+1]),this._$AH[a]=d}o&&!r&&this.j(t)}j(t){t===m?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}},ct=class extends U{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===m?void 0:t}},lt=class extends U{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==m)}},ht=class extends U{constructor(t,e,s,r,i){super(t,e,s,r,i),this.type=5}_$AI(t,e=this){if((t=H(this,t,e,0)??m)===T)return;let s=this._$AH,r=t===m&&s!==m||t.capture!==s.capture||t.once!==s.once||t.passive!==s.passive,i=t!==m&&(s===m||r);r&&this.element.removeEventListener(this.name,this,s),i&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){typeof this._$AH=="function"?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}},dt=class{constructor(t,e,s){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=s}get _$AU(){return this._$AM._$AU}_$AI(t){H(this,t)}};var Gt=pt.litHtmlPolyfillSupport;Gt?.(I,V),(pt.litHtmlVersions??=[]).push("3.3.3");var Tt=(n,t,e)=>{let s=e?.renderBefore??t,r=s._$litPart$;if(r===void 0){let i=e?.renderBefore??null;s._$litPart$=r=new V(t.insertBefore(W(),i),i,void 0,e??{})}return r._$AI(n),r};var _t=globalThis,C=class extends A{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){let t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){let e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=Tt(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return T}};C._$litElement$=!0,C.finalized=!0,_t.litElementHydrateSupport?.({LitElement:C});var Qt=_t.litElementPolyfillSupport;Qt?.({LitElement:C});(_t.litElementVersions??=[]).push("4.2.2");var Ot=n=>(t,e)=>{e!==void 0?e.addInitializer(()=>{customElements.define(n,t)}):customElements.define(n,t)};var Xt={attribute:!0,type:String,converter:D,reflect:!1,hasChanged:Q},te=(n=Xt,t,e)=>{let{kind:s,metadata:r}=e,i=globalThis.litPropertyMetadata.get(r);if(i===void 0&&globalThis.litPropertyMetadata.set(r,i=new Map),s==="setter"&&((n=Object.create(n)).wrapped=!0),i.set(e.name,n),s==="accessor"){let{name:o}=e;return{set(c){let a=t.get.call(this);t.set.call(this,c),this.requestUpdate(o,a,n,!0,c)},init(c){return c!==void 0&&this.C(o,void 0,n,c),c}}}if(s==="setter"){let{name:o}=e;return function(c){let a=this[o];t.call(this,c),this.requestUpdate(o,a,n,!0,c)}}throw Error("Unsupported decorator location: "+s)};function K(n){return(t,e)=>typeof e=="object"?te(n,t,e):((s,r,i)=>{let o=r.hasOwnProperty(i);return r.constructor.createProperty(i,s),o?Object.getOwnPropertyDescriptor(r,i):void 0})(n,t,e)}function k(n){return K({...n,state:!0,attribute:!1})}var Pt=["mon","tue","wed","thu","fri","sat","sun"],ee={mon:"Pon",tue:"Wt",wed:"\u015Ar",thu:"Czw",fri:"Pt",sat:"Sob",sun:"Nd"},Ht={"D+1":"#2ec4b6","D+2":"#7b6cf6","D+3":"#c98a3a"},se={charge:"#43a047",discharge:"#c98a3a",passthrough:"#6b6b6b"},O=880,N=250,P=48,Z=48,v=14,et=52,$=class extends C{constructor(){super(...arguments);this.narrow=!1;this._tab="overview";this._plan=null;this._status=null;this._log=[];this._profiles=null;this._forecasts=null;this._series=null;this._error=null}connectedCallback(){super.connectedCallback(),this._refresh(),this._timer=window.setInterval(()=>this._refresh(),6e4)}disconnectedCallback(){this._timer&&window.clearInterval(this._timer),super.disconnectedCallback()}async _refresh(){if(this.hass)try{let[e,s,r,i,o]=await Promise.all([this.hass.callWS({type:"powerpilot/plan"}),this.hass.callWS({type:"powerpilot/status"}),this.hass.callWS({type:"powerpilot/log"}),this.hass.callWS({type:"powerpilot/profiles"}),this.hass.callWS({type:"powerpilot/series",past_hours:24})]);this._plan=e,this._status=s,this._log=r?.events??[],this._profiles=i,this._series=o,this._error=null}catch(e){this._error=e?.message??String(e)}}async _loadForecasts(){if(!(this._forecasts||!this.hass))try{this._forecasts=await this.hass.callWS({type:"powerpilot/forecasts"})}catch(e){this._error=e?.message??String(e)}}_selectTab(e){this._tab=e,e==="profiles"&&this._loadForecasts()}_openConfig(){window.location.assign("/config/integrations/integration/powerpilot")}render(){return p`
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
      ${this._error?p`<div class="error">Błąd: ${this._error}</div>`:m}
      <div class="content">
        ${this._tab==="overview"?this._renderOverview():m}
        ${this._tab==="status"?this._renderStatus():m}
        ${this._tab==="profiles"?this._renderProfiles():m}
        ${this._tab==="logs"?this._renderLogs():m}
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
        <div class="card-title">Bateria (SoC %) i zużycie — realne dane → prognoza</div>
        ${this._series?this._socChart(this._series):p`<div class="empty">Ładowanie…</div>`}
        ${this._socLegend()}
      </div>
      <div class="card">
        <div class="card-title">Ceny (PLN/kWh) — pewne vs prognozowane + cena w baterii</div>
        ${this._series?this._priceChart(this._series):p`<div class="empty">Ładowanie…</div>`}
        ${this._priceLegend()}
      </div>
    `}_stat(e,s){return p`<div class="stat"><span class="k">${e}</span><span class="v">${s}</span></div>`}_niceTicks(e,s,r=5){s<=e&&(s=e+1);let o=(s-e)/r,c=Math.pow(10,Math.floor(Math.log10(o))),a=o/c,d=(a>=5?5:a>=2?2:1)*c,u=Math.ceil(e/d)*d,l=[];for(let g=u;g<=s+1e-9;g+=d)l.push(Math.round(g*1e3)/1e3);return l}_xAt(e,s){let r=O-P-Z;return s<=1?P+r/2:P+e*r/(s-1)}_xAxis(e){let s=e.length,r=v+(N-v-et),i=[];return e.forEach((o,c)=>{let a=this._xAt(c,s);o.getHours()===0&&(i.push(y`<line x1=${a} y1=${v} x2=${a} y2=${r} stroke="var(--divider-color)" stroke-width="1" opacity="0.7" />`),i.push(y`<text x=${a+3} y=${r+30} class="ax day">${o.getDate()}.${o.getMonth()+1}</text>`)),o.getHours()%3===0&&i.push(y`<text x=${a} y=${r+14} class="ax xh" text-anchor="middle">${String(o.getHours()).padStart(2,"0")}</text>`)}),i}_yAxisLeft(e,s,r){let i=[];return i.push(y`<text x=${P} y=${v-2} class="ax unit" text-anchor="start">${r}</text>`),e.forEach(o=>{let c=s(o);i.push(y`<line x1=${P} y1=${c} x2=${O-Z} y2=${c} stroke="var(--divider-color)" stroke-width="0.5" opacity="0.5" />`),i.push(y`<text x=${P-6} y=${c+3} class="ax" text-anchor="end">${o}</text>`)}),i}_yAxisRight(e,s,r){let i=[];return i.push(y`<text x=${O-Z} y=${v-2} class="ax unit" text-anchor="end">${r}</text>`),e.forEach(o=>{let c=s(o);i.push(y`<text x=${O-Z+6} y=${c+3} class="ax" text-anchor="start">${o}</text>`)}),i}_nowMarker(e,s){if(s<0)return m;let r=this._xAt(s,e.length),i=v+(N-v-et);return y`
      <line x1=${r} y1=${v} x2=${r} y2=${i} stroke="var(--primary-text-color)" stroke-width="1.5" stroke-dasharray="3 2" />
      <text x=${r+4} y=${v+12} class="ax now">Prognoza ▶</text>`}_path(e){return e.map((s,r)=>`${r?"L":"M"}${s.x.toFixed(1)},${s.y.toFixed(1)}`).join(" ")}_linePath(e,s,r,i,o){let c=e.length;if(c<2)return"";let a=r-s||1,d=6,u=o-d*2,l="",g=!1;return e.forEach((f,b)=>{if(isNaN(f)){g=!1;return}let _=b/(c-1)*i,S=d+u-(f-s)/a*u;l+=`${g?"L":"M"}${_.toFixed(1)},${S.toFixed(1)} `,g=!0}),l.trim()}_socChart(e){let s=e.hours,r=s.length;if(!r)return p`<div class="empty">Brak danych szeregu.</div>`;let i=s.map(h=>new Date(h.start)),o=N-v-et,c=v+o,a=s.flatMap(h=>[h.consumption_real,h.consumption_forecast,h.battery_charge_kwh,h.battery_discharge_kwh]).filter(h=>h!=null),d=Math.max(.5,...a),u=h=>c-h/100*o,l=h=>c-h/d*o,g=s.findIndex(h=>!h.is_past),f=Math.max(2,(O-P-Z)/r*.5),b=f/2,_=s.map((h,w)=>h.consumption_real==null?m:y`<rect x=${(this._xAt(w,r)-f/2).toFixed(1)} y=${l(h.consumption_real).toFixed(1)}
            width=${b.toFixed(1)} height=${(c-l(h.consumption_real)).toFixed(1)}
            fill="#b5475d" opacity="0.6" />`),S=s.map((h,w)=>h.battery_charge_kwh?y`<rect x=${this._xAt(w,r).toFixed(1)} y=${l(h.battery_charge_kwh).toFixed(1)}
            width=${b.toFixed(1)} height=${(c-l(h.battery_charge_kwh)).toFixed(1)}
            fill="#c98a3a" opacity="0.75" />`:m),L=s.map((h,w)=>h.battery_discharge_kwh?y`<rect x=${this._xAt(w,r).toFixed(1)} y=${l(h.battery_discharge_kwh).toFixed(1)}
            width=${b.toFixed(1)} height=${(c-l(h.battery_discharge_kwh)).toFixed(1)}
            fill="#b0a14f" opacity="0.75" />`:m),st=s.map((h,w)=>h.consumption_forecast==null?null:{x:this._xAt(w,r),y:l(h.consumption_forecast)}).filter(h=>h!=null),Ut=s.map((h,w)=>h.soc==null?null:{x:this._xAt(w,r),y:u(h.soc)}).filter(h=>h!=null),Nt=s.map((h,w)=>h.inverter_mode?y`<rect x=${(this._xAt(w,r)-f/2).toFixed(1)} y=${c+34}
            width=${f.toFixed(1)} height="7" rx="1" fill=${se[h.inverter_mode]??"#888"} />`:m);return y`
      <svg viewBox="0 0 ${O} ${N}" class="chart">
        ${this._yAxisLeft([0,25,50,75,100],u,"SoC %")}
        ${this._yAxisRight(this._niceTicks(0,d,4),l,"kWh")}
        ${this._xAxis(i)}
        ${_}
        ${S}
        ${L}
        <path d=${this._path(st)} fill="none" stroke="#e08aa0" stroke-width="1.5" stroke-dasharray="4 3" />
        <path d=${this._path(Ut)} fill="none" stroke="#2ec4b6" stroke-width="2.5" />
        ${Nt}
        ${this._nowMarker(i,g)}
      </svg>`}_priceChart(e){let s=e.hours,r=s.length;if(!r)return p`<div class="empty">Brak danych szeregu.</div>`;let i=s.map(_=>new Date(_.start)),o=N-v-et,c=v+o,a=s.flatMap(_=>[_.buy_price,_.battery_energy_cost]).filter(_=>_!=null),d=Math.min(0,...a),u=Math.max(.1,...a),l=_=>c-(_-d)/(u-d)*o,g=s.findIndex(_=>!_.is_past),f=[];for(let _=1;_<r;_++){let S=s[_-1],L=s[_];if(S.buy_price==null||L.buy_price==null)continue;let st=!L.price_confirmed;f.push(y`<line x1=${this._xAt(_-1,r).toFixed(1)} y1=${l(S.buy_price).toFixed(1)}
          x2=${this._xAt(_,r).toFixed(1)} y2=${l(L.buy_price).toFixed(1)}
          stroke="#2ec4b6" stroke-width="2" stroke-dasharray=${st?"4 3":"0"} />`)}let b=s.map((_,S)=>_.battery_energy_cost==null?null:{x:this._xAt(S,r),y:l(_.battery_energy_cost)}).filter(_=>_!=null);return y`
      <svg viewBox="0 0 ${O} ${N}" class="chart">
        ${this._yAxisLeft(this._niceTicks(d,u,5),l,"PLN/kWh")}
        ${this._xAxis(i)}
        ${f}
        <path d=${this._path(b)} fill="none" stroke="var(--secondary-text-color, #9e9e9e)" stroke-width="2" stroke-dasharray="2 2" />
        ${this._nowMarker(i,g)}
      </svg>`}_socLegend(){return p`<div class="fc-legend">
      <span class="fc-key"><span class="swatch" style="background:#2ec4b6"></span>SoC (%)</span>
      <span class="fc-key"><span class="swatch" style="background:#b5475d"></span>Zużycie realne (kWh)</span>
      <span class="fc-key"><span class="swatch" style="background:#e08aa0"></span>Zużycie prognoza (kWh)</span>
      <span class="fc-key"><span class="swatch" style="background:#c98a3a"></span>Ładowanie z sieci (kWh)</span>
      <span class="fc-key"><span class="swatch" style="background:#b0a14f"></span>Rozładowanie (kWh)</span>
      <span class="fc-key"><span class="swatch" style="background:#43a047"></span>ład.</span>
      <span class="fc-key"><span class="swatch" style="background:#c98a3a"></span>rozład.</span>
      <span class="fc-key"><span class="swatch" style="background:#6b6b6b"></span>przepływ</span>
    </div>`}_priceLegend(){return p`<div class="fc-legend">
      <span class="fc-key"><span class="swatch" style="background:#2ec4b6"></span>Cena zakupu — ciągła = pewna, przerywana = prognoza</span>
      <span class="fc-key"><span class="swatch" style="background:#9e9e9e"></span>Cena w baterii (po stratach)</span>
    </div>`}_renderStatus(){let e=this._status;return e?p`
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
            ${s.error?p`<span class="muted">${s.error}</span>`:m}
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
    `}_heatmap(e,s){let r=[];if(Pt.forEach(c=>(e[c]??[]).forEach(a=>{a!=null&&r.push(a)})),!r.length)return p`<div class="empty">Brak danych — profil jeszcze się uczy.</div>`;let i=Math.min(...r),o=Math.max(...r);return p`
      <div class="heatmap">
        <div class="hm-row hm-head">
          <div class="hm-label"></div>
          ${Array.from({length:24},(c,a)=>p`<div class="hm-h">${a}</div>`)}
        </div>
        ${Pt.map(c=>p`
            <div class="hm-row">
              <div class="hm-label">${ee[c]}</div>
              ${(e[c]??[]).map(a=>{let d=a==null?"transparent":this._heatColor(a,i,o),u=a==null?"\u2014":`${a.toFixed(3)} ${s}`;return p`<div class="hm-cell" style=${"background:"+d} title=${u}></div>`})}
            </div>
          `)}
      </div>
      <div class="legend">
        <span>${i.toFixed(2)}</span>
        <div class="legend-bar"></div>
        <span>${o.toFixed(2)} ${s}</span>
      </div>
    `}_heatColor(e,s,r){return`hsl(${(1-(r>s?(e-s)/(r-s):.5))*160}, 70%, 45%)`}_renderForecastOverlay(){let e=this._forecasts;if(!e)return p`<div class="empty">Ładowanie prognoz…</div>`;let s=Object.keys(e.horizons||{});if(!s.length)return p`<div class="empty">Brak prognoz (wymaga źródła Pradcast z kluczem API).</div>`;let r=l=>{let g=new Array(24).fill(NaN);return l.forEach(f=>{f.buy!==null&&f.hour>=0&&f.hour<24&&(g[f.hour]=f.buy)}),g},i=s.map(l=>({h:l,vals:r(e.horizons[l])})),o=i.flatMap(l=>l.vals).filter(l=>!isNaN(l)),c=Math.min(0,...o),a=Math.max(.1,...o),d=760,u=180;return p`
      <svg viewBox="0 0 ${d} ${u}" class="chart">
        ${i.map(l=>y`<path d=${this._linePath(l.vals,c,a,d,u)} fill="none"
              stroke=${Ht[l.h]??"#888"} stroke-width="2" />`)}
      </svg>
      <div class="fc-legend">
        ${i.map(l=>p`<span class="fc-key">
            <span class="swatch" style=${"background:"+(Ht[l.h]??"#888")}></span>${l.h}
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
    </div>`:p`<div class="card empty">Brak zdarzeń.</div>`}_time(e){try{return new Date(e).toLocaleString()}catch{return e}}};$.styles=it`
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
    .ax {
      fill: var(--secondary-text-color);
      font-size: 10px;
    }
    .ax.unit {
      font-weight: 600;
    }
    .ax.day {
      font-weight: 600;
    }
    .ax.now {
      fill: var(--primary-text-color);
      font-weight: 600;
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
  `,x([K({attribute:!1})],$.prototype,"hass",2),x([K({attribute:!1})],$.prototype,"narrow",2),x([k()],$.prototype,"_tab",2),x([k()],$.prototype,"_plan",2),x([k()],$.prototype,"_status",2),x([k()],$.prototype,"_log",2),x([k()],$.prototype,"_profiles",2),x([k()],$.prototype,"_forecasts",2),x([k()],$.prototype,"_series",2),x([k()],$.prototype,"_error",2),$=x([Ot("powerpilot-panel")],$);export{$ as PowerPilotPanel};
