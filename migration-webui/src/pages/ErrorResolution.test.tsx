import { fireEvent,render,screen,waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach,describe,expect,it,vi } from 'vitest'
import ErrorResolution from './ErrorResolution'

const api=vi.hoisted(()=>({fetchErrorResolution:vi.fn(),fetchErrorCase:vi.fn(),errorCaseAction:vi.fn(),configureErrorResolution:vi.fn(),connectErrorRunner:vi.fn(),revokeErrorRunner:vi.fn()}))
vi.mock('@/api/controlPlane',()=>api)
const example={id:7,title:'TypeError: response shape',category:'code',service:'gmail',reason:'TypeError',state:'FAILED',
  occurrences:4,affected_users:2,affected_items:3,version:'abc1234',last_seen:'2026-11-05T12:00:00Z',next_action:'Regression failed; inspect evidence.',candidate_commit:null}
const view=(extra={})=>({cases:[example],policy:{enabled:1,daily_limit:3},runners:[],canManage:true,canManageGlobal:false,...extra})
const show=()=>render(<MemoryRouter><ErrorResolution/></MemoryRouter>)
beforeEach(()=>{
  Object.values(api).forEach(f=>f.mockReset())
  api.fetchErrorResolution.mockResolvedValue(view())
  api.errorCaseAction.mockResolvedValue({ok:true,detail:'Accepted'})
})
describe('Error resolution',()=>{
  it('shows grouped scope, disconnected worker and limits without claiming resolution',async()=>{
    show()
    expect(await screen.findByTestId('error-case-7')).toHaveTextContent('4 occurrences · 2 users · 3 affected items')
    expect(screen.getByText('Not connected')).toBeInTheDocument()
    expect(screen.getByText(/three|two attempts per case/)).toBeInTheDocument()
    expect(screen.getByText(/sample, matching count/)).toBeInTheDocument()
    expect(screen.queryByRole('button',{name:'Resolve'})).not.toBeInTheDocument()
  })
  it('requires a reason and explicitly describes a read-only recheck',async()=>{
    show()
    fireEvent.click(await screen.findByRole('button',{name:'Recheck affected data'}))
    expect(screen.getByText(/No target repair is enabled/)).toBeInTheDocument()
    expect(screen.getByRole('button',{name:'Confirm'})).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Reason Code'),{target:{value:'Verify repaired scope'}})
    fireEvent.click(screen.getByRole('button',{name:'Confirm'}))
    await waitFor(()=>expect(api.errorCaseAction).toHaveBeenCalledWith(7,'recheck','Verify repaired scope'))
  })
  it('does not expose write or runner controls to viewers',async()=>{
    api.fetchErrorResolution.mockResolvedValue(view({canManage:false}))
    show();await screen.findByTestId('error-case-7')
    expect(screen.queryByRole('button',{name:'Connect coding worker'})).not.toBeInTheDocument()
    expect(screen.queryByRole('button',{name:'Queue investigation'})).not.toBeInTheDocument()
    expect(screen.queryByRole('button',{name:'Recheck affected data'})).not.toBeInTheDocument()
  })
  it('shows history and escaped evidence from the authorised API',async()=>{
    api.fetchErrorCase.mockResolvedValue({...example,history:[{state:'TESTING',detail:'Regression fails before fix',at:'2026-11-05T12:00:00Z'}],
      affected:[],attempts:[],events:[{message:'<script>untrusted</script>'}]})
    show();fireEvent.click(await screen.findByRole('button',{name:'View evidence'}))
    expect(await screen.findByText(/Regression fails before fix/)).toBeInTheDocument()
    expect(document.querySelector('script')).toBeNull()
  })
  it('reports failed reads instead of showing a successful empty state',async()=>{
    api.fetchErrorResolution.mockRejectedValue(new Error('Unavailable'))
    show();expect(await screen.findByText(/Unavailable/)).toBeInTheDocument()
    expect(screen.queryByText(/No matching cases recorded/)).not.toBeInTheDocument()
  })
  it('keeps resolved cases hidden unless requested',async()=>{
    api.fetchErrorResolution.mockResolvedValue(view({cases:[{...example,state:'RESOLVED'}]}))
    show();await screen.findByText(/No matching cases/)
    fireEvent.click(screen.getByRole('checkbox',{name:'Show resolved'}))
    expect(await screen.findByTestId('error-case-7')).toHaveTextContent('Verified resolved')
  })
  it('offers qualification retry for an existing held candidate',async()=>{
    api.fetchErrorResolution.mockResolvedValue(view({cases:[{...example,state:'WAITING_QUALIFICATION',candidate_commit:'b'.repeat(40)}]}))
    show()
    fireEvent.click(await screen.findByRole('button',{name:'Retry qualification'}))
    fireEvent.change(screen.getByLabelText('Reason Code'),{target:{value:'Retry candidate qualification'}})
    fireEvent.click(screen.getByRole('button',{name:'Confirm'}))
    await waitFor(()=>expect(api.errorCaseAction).toHaveBeenCalledWith(7,'queue','Retry candidate qualification'))
  })
})
