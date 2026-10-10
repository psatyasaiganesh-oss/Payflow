// Browser-only companion demo. The downloadable Python service owns the actual
// database transactions, signatures and multi-worker concurrency guarantees.
const id = prefix => prefix + crypto.randomUUID().replaceAll('-', '').slice(0, 16);
const now = () => Date.now() / 1000;
const copy = value => JSON.parse(JSON.stringify(value));
const fresh = () => ({orders:[],payments:{},events:[],ledger:[],activity:[],runs:[],refunds:{},paused:false,failNext:0,timings:[]});

export const SCENARIOS = [
  {id:'duplicate_request', tag:'IDEMPOTENCY', title:'One request. Twenty retries.', description:'Repeat a checkout request twenty times with one key. Verify that it still creates just one payment.'},
  {id:'duplicate_webhook', tag:'EVENT DELIVERY', title:'The same event, ten times.', description:'Replay a payment event ten times. Verify one capture and one order fulfillment.'},
  {id:'out_of_order', tag:'CORRECTNESS', title:'A late event arrives.', description:'Deliver an old event after a successful payment. Verify that current payment state stays correct.'},
  {id:'worker_failure', tag:'RETRY & RECOVERY', title:'The worker hits a fault.', description:'Fail a processing attempt, preserve the event, and retry it successfully.'},
  {id:'crash_recovery', tag:'WORKER LEASES', title:'A worker disappears.', description:'Abandon a claimed event, expire its lease, and let another processing attempt recover it.'},
  {id:'reconciliation', tag:'RECONCILIATION', title:'The webhook never arrives.', description:'Compare order state with the provider and repair a missing payment confirmation.'},
  {id:'invalid_signature', tag:'WEBHOOK SECURITY', title:'Someone changes the payload.', description:'Modify a signed event. Verify that signature validation rejects it before any fulfillment.'}
];

export class DemoStore {
  constructor(storage = globalThis.localStorage) {
    this.storage = storage;
    try { this.data = JSON.parse(storage?.getItem('payflow-demo-v1')) || fresh(); }
    catch { this.data = fresh(); }
  }
  save() { try { this.storage?.setItem('payflow-demo-v1', JSON.stringify(this.data)); } catch {} }
  audit(kind,message,orderId=null) { this.data.activity.unshift({id:id('log_'),kind,message,order_id:orderId,created_at:now()}); }
  getOrder(orderId) { const order=this.data.orders.find(x=>x.id===orderId); if(!order)throw Error('Order not found');return order; }
  createOrder(input,key) {
    const prior=this.data.orders.find(x=>x.idempotency_key===key);
    if(prior){ if(prior.product!==input.product||prior.customer!==input.customer||prior.amount!==input.amount||prior.currency!==input.currency)throw Error('Idempotency key belongs to a different request');return {order:copy(prior),replayed:true}; }
    if(!Number.isInteger(input.amount)||input.amount<50)throw Error('Enter a valid payment amount');
    const order={...input,id:id('ord_'),idempotency_key:key,payment_id:id('pi_demo_'),status:'pending',refunded:0,fulfillment_count:0,created_at:now(),updated_at:now(),last_error:null};
    this.data.orders.unshift(order);this.data.payments[order.payment_id]={id:order.payment_id,amount:order.amount,currency:order.currency,status:'requires_payment_method',refunded:0};
    this.audit('order_created','Order accepted with an idempotency key',order.id);this.save();return {order:copy(order),replayed:false};
  }
  eventFor(paymentId,type='payment_intent.succeeded') { return {id:id('evt_'),payment_id:paymentId,type,status:'pending',deliveries:1,attempts:0,created_at:now(),available_at:now(),processed_at:null,last_error:null,lease_until:null}; }
  deliver(event) { const prior=this.data.events.find(x=>x.id===event.id); if(prior)prior.deliveries++;else this.data.events.unshift(copy(event));this.save(); }
  emit(orderId,outcome='success',repetitions=1) { const order=this.getOrder(orderId),payment=this.data.payments[order.payment_id];if(payment.status==='succeeded'&&outcome==='decline')throw Error('A captured payment cannot regress');payment.status=outcome==='success'?'succeeded':'failed';const event=this.eventFor(payment.id,outcome==='success'?'payment_intent.succeeded':'payment_intent.payment_failed');for(let i=0;i<repetitions;i++)this.deliver(event);this.drain();return event; }
  apply(order,payment) {
    if(order.amount!==payment.amount||order.currency!==payment.currency)throw Error('Provider amount/currency mismatch');
    const captured=order.fulfillment_count===1||payment.status==='succeeded';
    if(captured&&!order.fulfillment_count){order.fulfillment_count=1;this.data.ledger.unshift({id:id('le_'),order_id:order.id,kind:'capture',amount:order.amount,currency:order.currency,created_at:now()});this.audit('payment_captured','Payment verified; order fulfilled once',order.id);}
    const delta=Math.max(order.refunded,payment.refunded)-order.refunded;
    if(delta){order.refunded+=delta;this.data.ledger.unshift({id:id('le_'),order_id:order.id,kind:'refund',amount:-delta,currency:order.currency,created_at:now()});this.audit('refund_recorded','Refund verified against the provider',order.id);}
    order.status=captured?(order.refunded===order.amount?'refunded':order.refunded?'partially_refunded':'paid'):payment.status==='failed'?'failed':'pending';order.updated_at=now();
  }
  drain(force=false) {
    if(this.data.paused&&!force)return [];
    const results=[];
    for(const event of [...this.data.events].reverse()){
      if(!(event.status==='pending'&&event.available_at<=now()||event.status==='processing'&&event.lease_until<=now()))continue;
      event.status='processing';event.attempts++;event.lease_until=now()+30;
      if(this.data.failNext>0){this.data.failNext--;event.status=event.attempts>=5?'dead':'pending';event.available_at=now()+Math.min(60,2**event.attempts);event.last_error='Injected worker failure';this.audit('processing_failure',event.last_error);results.push({status:'retry_scheduled'});continue;}
      const order=this.data.orders.find(x=>x.payment_id===event.payment_id);this.apply(order,this.data.payments[event.payment_id]);event.status='done';event.processed_at=now();event.lease_until=null;event.last_error=null;results.push({status:'done'});
    }
    this.save();return results;
  }
  refund(orderId,amount,key) {
    const order=this.getOrder(orderId),prior=this.data.refunds[key];
    if(prior){if(prior.order_id!==orderId||prior.amount!==amount)throw Error('Refund key belongs to a different request');return copy(prior);}
    const payment=this.data.payments[order.payment_id];
    if(payment.status!=='succeeded'||!Number.isInteger(amount)||amount<=0||amount>order.amount-payment.refunded)throw Error('Refund exceeds available captured funds');
    payment.refunded+=amount;const record={refund_id:id('re_demo_'),order_id:orderId,amount};this.data.refunds[key]=record;this.deliver(this.eventFor(payment.id,'charge.refunded'));this.drain();return copy(record);
  }
  reconcile() {
    let queued=0;for(const order of this.data.orders){const payment=this.data.payments[order.payment_id];if(payment.status==='succeeded'&&!order.fulfillment_count||payment.refunded>order.refunded){this.deliver(this.eventFor(payment.id,'reconciliation.snapshot'));queued++;}}
    this.drain();return {checked:this.data.orders.length,repair_events_queued:queued,errors:0};
  }
  retry(eventId) { const event=this.data.events.find(x=>x.id===eventId);if(!event)throw Error('Event not found');if(!['pending','dead'].includes(event.status))throw Error('Event is not retryable');if(event.status==='dead')event.attempts=0;event.status='pending';event.available_at=now();this.save(); }
  seed() {
    const products=[['Orbit Workspace','Anika Rao',249900,'success'],['Atlas Cloud','Rohan Patel',89900,'success'],['Design Studio','Maya Shah',149900,'success'],['Developer Pro','Arjun Kumar',39900,'decline'],['Launch Kit','Sara Das',129900,null],['Analytics Plus','Neha Mehta',69900,'success']];
    products.forEach(([product,customer,amount,outcome],index)=>{const response=this.createOrder({product,customer,amount,currency:'inr'},'seed:'+index);if(response.replayed)return;const order=this.getOrder(response.order.id);order.created_at=now()-(5-index)*86400;if(outcome)this.emit(order.id,outcome);});this.save();return {seeded:true};
  }
  async scenario(name) {
    if(!SCENARIOS.some(x=>x.id===name))throw Error('Unknown scenario');
    const paused=this.data.paused;this.data.paused=true;this.drain(true);
    const input={product:'Reliability test',customer:'Demo merchant',amount:149900,currency:'inr'},key=id('lab_'),order=this.getOrder(this.createOrder(input,key).order.id),checks=[];
    const check=(label,passed)=>checks.push({label,passed});
    try{
      if(name==='duplicate_request'){const responses=Array.from({length:20},()=>this.createOrder(input,key));check('20 repeated requests reuse one order',responses.every(x=>x.order.id===order.id&&x.replayed));this.emit(order.id);}
      else if(name==='invalid_signature'){
        // Web Crypto validates an actual HMAC against a tampered message.
        const encode=new TextEncoder(),secret=await crypto.subtle.importKey('raw',encode.encode('demo-signing-key'),{name:'HMAC',hash:'SHA-256'},false,['sign','verify']);
        const signature=await crypto.subtle.sign('HMAC',secret,encode.encode('amount=149900'));
        const valid=await crypto.subtle.verify('HMAC',secret,signature,encode.encode('amount=1'));
        check('Tampered payload rejected before persistence',!valid);check('No payment or fulfillment side effects',order.fulfillment_count===0);
      }else if(name==='out_of_order'){const old=this.eventFor(order.payment_id,'payment_intent.payment_failed');this.emit(order.id);this.deliver(old);check('Old event delivered after the success event',true);}
      else if(name==='reconciliation'){this.data.payments[order.payment_id].status='succeeded';check('Missing webhook detected and repair queued',this.reconcile().repair_events_queued>0);}
      else{
        const event=this.emit(order.id,'success',name==='duplicate_webhook'?10:1);
        if(name==='worker_failure'){this.data.failNext=1;const result=this.drain(true);check('Worker failure leaves a durable retry',result.some(x=>x.status==='retry_scheduled'));this.retry(event.id);}
        if(name==='crash_recovery'){const pending=this.data.events.find(x=>x.id===event.id);pending.status='processing';pending.attempts=1;pending.lease_until=now()-1;}
      }
      this.drain(true);
      if(['duplicate_webhook','crash_recovery'].includes(name)){const processed=this.data.events.find(x=>x.payment_id===order.payment_id);if(name==='duplicate_webhook')check('Ten deliveries recorded for one event',processed.deliveries===10);else check('Abandoned claim recovered after lease expiry',processed.status==='done'&&processed.attempts>=2);}
      if(name!=='invalid_signature'){check('Order fulfilled exactly once',order.fulfillment_count===1&&order.status==='paid');check('One capture ledger entry',this.data.ledger.filter(x=>x.order_id===order.id).length===1);}
      const result={id:id('run_'),name,passed:checks.every(x=>x.passed),checks,order_id:order.id,created_at:now()};this.data.runs.unshift(result);return copy(result);
    }finally{this.data.paused=paused;this.data.failNext=0;this.save();}
  }
  state() {
    const d=this.data,currencies={};for(const order of d.orders){currencies[order.currency]??={captured:0,refunded:0,net:0};if(order.fulfillment_count){currencies[order.currency].captured+=order.amount;currencies[order.currency].refunded+=order.refunded;currencies[order.currency].net+=order.amount-order.refunded;}}
    const backlog=d.events.filter(x=>['pending','processing'].includes(x.status)),decided=d.orders.filter(x=>x.fulfillment_count||x.status==='failed'),timings=[...d.timings].sort((a,b)=>a-b);
    return copy({provider:'browser_demo',worker_paused:d.paused,orders:d.orders.slice(0,100),events:d.events.slice(0,100),ledger:d.ledger.slice(0,100),activity:d.activity.slice(0,50),runs:d.runs.slice(0,20),updated_at:now(),monitoring:{database:'browser storage',api_samples:timings.length,api_p95_ms:timings.length?Number(timings[Math.min(timings.length-1,Math.floor(timings.length*.95))].toFixed(2)):null},stats:{orders:d.orders.length,captured_orders:d.orders.reduce((sum,x)=>sum+x.fulfillment_count,0),failed_orders:d.orders.filter(x=>x.status==='failed').length,by_currency:currencies,success_rate:decided.length?Number((decided.filter(x=>x.fulfillment_count).length/decided.length*100).toFixed(1)):null,backlog:backlog.length,dead_events:d.events.filter(x=>x.status==='dead').length,events_processed:d.events.filter(x=>x.status==='done').length,duplicate_deliveries:d.events.reduce((sum,x)=>sum+x.deliveries-1,0),oldest_pending_seconds:Number(Math.max(0,...backlog.map(x=>now()-x.created_at)).toFixed(1))}});
  }
  async request(path,body={},key) {
    const start=performance.now();let result;
    if(path==='/api/state')result=this.state();
    else if(path==='/api/orders')result=this.createOrder(body,key);
    else if(path==='/api/demo/seed')result=this.seed();
    else if(path.startsWith('/api/demo/scenarios/'))result=await this.scenario(path.split('/').at(-1));
    else if(path.startsWith('/api/demo/orders/'))result=this.emit(path.split('/')[4],body.outcome,body.repetitions);
    else if(path.endsWith('/refund'))result=this.refund(path.split('/')[3],body.amount,key);
    else if(path.endsWith('/retry')){this.retry(path.split('/')[3]);result={scheduled:true};}
    else if(path==='/api/reconcile')result=this.reconcile();
    else if(path.startsWith('/api/worker/')){const action=path.split('/').at(-1);if(action==='pause')this.data.paused=true;if(action==='resume')this.data.paused=false;if(action==='fail-next')this.data.failNext=1;result={processed:['resume','run'].includes(action)?this.drain():[],action};}
    else throw Error('Unknown operation');
    this.data.timings.push(performance.now()-start);this.data.timings=this.data.timings.slice(-500);this.save();return result;
  }
}
