import test from 'node:test';
import assert from 'node:assert/strict';
import {DemoStore,SCENARIOS} from '../web/demo-store.js';
const memory=()=>{const map=new Map();return {getItem:key=>map.get(key)||null,setItem:(key,value)=>map.set(key,value)};};
const data={product:'Developer Pro',customer:'Ganesh',amount:149900,currency:'inr'};

for(const scenario of SCENARIOS)test(`browser companion: ${scenario.id}`,async()=>{
  const store=new DemoStore(memory());const result=await store.request('/api/demo/scenarios/'+scenario.id);
  assert.equal(result.passed,true,JSON.stringify(result.checks));
});
test('browser persistence and payment replay',()=>{
  const storage=memory(),store=new DemoStore(storage),first=store.createOrder(data,'same');
  const restarted=new DemoStore(storage),second=restarted.createOrder(data,'same');
  assert.equal(second.order.id,first.order.id);assert.equal(second.replayed,true);
  assert.throws(()=>restarted.createOrder({...data,amount:199900},'same'));
});
test('browser refund idempotency preserves balance',()=>{
  const store=new DemoStore(memory()),order=store.createOrder(data,'new').order;store.emit(order.id);
  store.refund(order.id,49900,'refund');store.refund(order.id,49900,'refund');
  assert.equal(store.getOrder(order.id).refunded,49900);assert.equal(store.state().stats.by_currency.inr.net,100000);
  assert.throws(()=>store.refund(order.id,100001,'over-refund'));
});
test('browser API routes support manual payment and worker recovery',async()=>{
  const store=new DemoStore(memory());await store.request('/api/worker/pause',{});
  const {order}=await store.request('/api/orders',data,'route-order');
  await store.request(`/api/demo/orders/${order.id}/emit`,{outcome:'success',repetitions:10});
  assert.equal(store.state().stats.backlog,1);await store.request('/api/worker/resume',{});
  assert.equal(store.getOrder(order.id).fulfillment_count,1);assert.equal(store.state().stats.duplicate_deliveries,9);
});
test('sample data seeding is repeatable without duplicate orders',()=>{
  const store=new DemoStore(memory());store.seed();store.seed();assert.equal(store.state().stats.orders,6);
});
