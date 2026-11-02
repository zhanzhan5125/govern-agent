import {SharedBudget} from '@earendil-works/pi-agent-core';

/** Quotas charge both the local account and its ancestor, never mint new funds. */
export class ScopedBudget extends SharedBudget {
 private readonly onParentAbort=()=>this.controller.abort(this.parent.controller.signal.reason);
 constructor(readonly parent:SharedBudget,limits:{tokens:number;turns:number;tools:number;milliseconds:number}){
  super(limits);parent.controller.signal.addEventListener('abort',this.onParentAbort);
 }
 override check(){this.parent.check();super.check();}
 override async reserveAsync(messages:unknown[],output:number,signal?:AbortSignal){
  const own=await super.reserveAsync(messages,output,signal);
  let ancestor:Awaited<ReturnType<SharedBudget['reserveAsync']>>;
  try{ancestor=await this.parent.reserveAsync(messages,output,signal);}catch(e){own.settle(0);throw e;}
  let settled=false;
  return {settle:(actual?:number)=>{if(settled)return;settled=true;try{own.settle(actual);}finally{ancestor.settle(actual);}}};
 }
 override takeTool(){super.takeTool();this.parent.takeTool();}
 override remainingTools(){return Math.min(super.remainingTools(),this.parent.remainingTools());}
 override tryTakeTool(){
  this.check();
  // Synchronous admission across all ancestors: rejection never charges a child
  // or aborts its siblings. No await between the check and ancestor debit.
  if(this.remainingTools()<1)return false;
  if(!this.parent.tryTakeTool())return false;
  this.tools++;return true;
 }
 dispose(){this.parent.controller.signal.removeEventListener('abort',this.onParentAbort);}
}
