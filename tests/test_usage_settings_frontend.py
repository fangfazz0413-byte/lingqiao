"""Settings interaction tests use synthetic keys and API responses only."""
import unittest
import test_cleanup_frontend as cleanup_fixture


class UsageSettingsFrontendTests(unittest.TestCase):
    run_js = cleanup_fixture.CleanupFrontendTests.run_js

    def test_loading_never_returns_or_prefills_key_material(self):
        self.run_js(r'''
          (async () => {
            await openUsageSettings();
            const html=$('#usage-provider-form').innerHTML;
            assert.match(html,/已配置/); assert.match(html,/type="password"/);
            assert.doesNotMatch(html,/value="/); assert.equal(usageSettings.busy,false);
          })()
        ''', setup="responder=async()=>({providers:[{name:'glm',label:'GLM',configured:true,hint:'official console'}]});")

    def test_save_partial_failure_preserves_dialog_and_reports_labels(self):
        self.run_js(r'''
          (async () => {
            await openUsageSettings(); await saveUsageSettings();
            assert.equal(usageSettings.visible,true);
            assert.match($('#usage-provider-form').innerHTML,/GLM保存失败/);
            assert.doesNotMatch($('#usage-provider-form').innerHTML,/synthetic-only-secret/);
            assert.equal($('#usage-settings-save').disabled,false);
          })()
        ''', setup=r'''
          document.querySelectorAll=selector=>selector==='#usage-provider-form [data-provider-key]'?[{value:'synthetic-only-secret',dataset:{providerKey:'glm'}}]:[];
          lookup('#usage-provider-form').insertAdjacentHTML=(_,html)=>{lookup('#usage-provider-form').innerHTML=html+lookup('#usage-provider-form').innerHTML;};
          responder=async(url,options)=>url==='/api/usage/providers'?{providers:[{name:'glm',label:'GLM',configured:true}]}:{ok:[],fail:[{label:'GLM',error:'保存失败'}]};
        ''')

    def test_theme_is_forwarded_to_native_window_bridge(self):
        self.run_js(r'''
          const received=[]; window.pywebview={api:{set_theme(theme){received.push(theme);return Promise.resolve(theme);}}};
          applyAppearance({theme:'cream',font:'round'});
          assert.equal(received.at(-1),'cream');
          applyAppearance({theme:'unexpected',font:'round'});
          assert.equal(received.at(-1),'rose');
        ''')


if __name__ == '__main__':
    unittest.main()
