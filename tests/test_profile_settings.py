"""Reviewed per-Blink settings metadata and deterministic operation discovery."""
import copy
import tempfile
import unittest

from blink.errors import BlinkError
from blink.profile import compile_manifest
from blink.runtime import Runtime


def setting_manifest():
    return {
        'openapi':'3.1.0','info':{'title':'Settings example','version':'1.0.0'},
        'servers':[{'url':'https://api.banana.example'}],
        'x-blink-service':{'id':'settings-example','requestedOrigins':['https://api.banana.example'],
            'auth':{'kind':'none'},'exec':{'allowed':['direct'],'preferred':'direct'},
            'settings':{'type':'object','additionalProperties':False,'properties':{
                'region':{'type':'string','enum':['us','eu'],'default':'eu'},
                'language':{'type':'string','default':'en'}
            }}},
        'paths':{'/products':{'get':{'operationId':'products','summary':'Read products','x-blink-effect':'read',
            'parameters':[{'name':'market','in':'query','schema':{'type':'string','enum':['us','eu']}},
                          {'name':'lang','in':'query','schema':{'type':'string'}}],
            'x-blink-setting-map':{'region':'market','language':'lang'},
            'responses':{'200':{'description':'Products','content':{'application/json':{'schema':{'type':'object'}}}}}}}}}


class ProfileSettingTests(unittest.TestCase):
    def test_publisher_cannot_choose_cli_or_local_account_controls(self):
        for key in ('binding', 'output', 'debug', 'dry_run', 'dry-run'):
            with self.subTest(key=key), self.assertRaises(BlinkError):
                doc=setting_manifest()
                doc['x-blink-service']['settings']['properties'][key]={'type':'string','default':'anything'}
                compile_manifest(doc)

    def test_metadata_is_compiled_and_default_is_not_an_implicit_argument(self):
        doc=setting_manifest();compiled=compile_manifest(doc);op=compiled['operations']['products']
        self.assertEqual(op['settingsSchema']['properties']['region']['default'],'eu')
        self.assertEqual(op['settingMap'],{'region':'market','language':'lang'})
        self.assertEqual(compiled['settingsSchema'],op['settingsSchema'])
        self.assertNotIn('default',op['inputSchema']['properties']['market'])
        self.assertEqual(doc,setting_manifest(),'Compiling must not alter caller-owned data')

    def test_setting_schema_default_and_mapping_changes_are_semantic(self):
        original=setting_manifest();old=compile_manifest(original)
        for mutation in ('default','mapping','schema'):
            with self.subTest(mutation=mutation):
                changed=copy.deepcopy(original)
                if mutation=='default':changed['x-blink-service']['settings']['properties']['region']['default']='us'
                elif mutation=='mapping':changed['paths']['/products']['get']['x-blink-setting-map']={'language':'lang'}
                else:changed['x-blink-service']['settings']['properties']['language']['maxLength']=20
                new=compile_manifest(changed)
                self.assertNotEqual(old['digest'],new['digest'])
                self.assertNotEqual(old['operations']['products']['digest'],new['operations']['products']['digest'])

    def test_unknown_setting_target_body_and_url_overrides_are_rejected(self):
        for mapping in ({'missing':'market'},{'region':'absent'},{'region':'body'},{'region':'origin'},{'region':'headers'}):
            with self.subTest(mapping=mapping),self.assertRaises(BlinkError):
                doc=setting_manifest();doc['paths']['/products']['get']['x-blink-setting-map']=mapping
                compile_manifest(doc)

    def test_protected_header_cannot_become_a_setting_target(self):
        doc=setting_manifest();op=doc['paths']['/products']['get']
        op['parameters'].append({'name':'Authorization','in':'header','schema':{'type':'string'}})
        op['x-blink-setting-map']={'language':'Authorization'}
        with self.assertRaises(BlinkError):compile_manifest(doc)

    def test_credential_setting_names_are_rejected_recursively(self):
        for name in ('api_key','authorization','password','refreshToken','credentials','private_key','session','key'):
            with self.subTest(name=name),self.assertRaises(BlinkError):
                doc=setting_manifest();doc['x-blink-service']['settings']['properties'][name]={'type':'string'}
                compile_manifest(doc)
        doc=setting_manifest()
        doc['x-blink-service']['settings']['properties']['preferences']={
            'type':'object','properties':{'secret':{'type':'string'}},'additionalProperties':False}
        with self.assertRaises(BlinkError):compile_manifest(doc)

    def test_invalid_open_or_unsafe_settings_schemas_are_rejected(self):
        for mutation in ('open','ref','pattern','required','default','nested_open','type_mismatch','duplicate_target'):
            with self.subTest(mutation=mutation),self.assertRaises(BlinkError):
                doc=setting_manifest();schema=doc['x-blink-service']['settings']
                if mutation=='open':schema['additionalProperties']=True
                elif mutation=='ref':schema['properties']['language']={'$ref':'https://invalid.example/schema'}
                elif mutation=='pattern':schema['properties']['language']['pattern']='.*'
                elif mutation=='required':schema['required']=['not-declared']
                elif mutation=='default':schema['properties']['region']['default']='unsupported-region'
                elif mutation=='nested_open':schema['properties']['prefs']={'type':'object','properties':{},'additionalProperties':True}
                elif mutation=='type_mismatch':schema['properties']['language']={'type':'integer','default':1}
                else:doc['paths']['/products']['get']['x-blink-setting-map']={'region':'lang','language':'lang'}
                compile_manifest(doc)

    def test_setting_default_must_fit_the_parameter_and_maps_must_be_scalar(self):
        doc=setting_manifest();doc['paths']['/products']['get']['parameters'][0]['schema']['enum']=['us']
        with self.assertRaises(BlinkError):compile_manifest(doc)
        doc=setting_manifest();doc['x-blink-service']['settings']['properties']['region']={'type':'array','items':{'type':'string'}}
        with self.assertRaises(BlinkError):compile_manifest(doc)

    def test_numeric_integer_setting_can_target_a_numeric_parameter(self):
        doc=setting_manifest();doc['x-blink-service']['settings']['properties']['limit']={'type':'integer','default':10}
        doc['paths']['/products']['get']['parameters'].append({'name':'pageSize','in':'query','schema':{'type':'number'}})
        doc['paths']['/products']['get']['x-blink-setting-map']['limit']='pageSize'
        self.assertEqual(compile_manifest(doc)['operations']['products']['settingMap']['limit'],'pageSize')

    def test_describe_exposes_settings_and_nonsetting_manifests_have_empty_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Runtime(tmp)
            result=runtime.install_manifest(setting_manifest(),'test','settings-example')['data']
            self.assertEqual(runtime.dispatch('approve_manifest',result,human=True)['status'],'ok')
            described=runtime.dispatch('describe',{'service':result['service'],'operation':'products'})
            self.assertEqual(described['status'],'ok')
            self.assertEqual(described['data']['settingMap']['region'],'market')
            self.assertFalse(described['data']['settingsSchema']['additionalProperties'])
        doc=setting_manifest();doc['x-blink-service'].pop('settings');doc['paths']['/products']['get'].pop('x-blink-setting-map')
        op=compile_manifest(doc)['operations']['products']
        self.assertEqual(op['settingMap'],{})
        self.assertEqual(op['settingsSchema']['properties'],{})

    def test_search_pages_are_bounded_complete_and_deterministically_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Runtime(tmp);doc=setting_manifest();template=copy.deepcopy(doc['paths']['/products']['get'])
            doc['paths']={}
            for index in reversed(range(230)):
                operation=copy.deepcopy(template);operation['operationId']=f'op_{index:03d}'
                doc['paths'][f'/p{index}']={'get':operation}
            stage=runtime.install_manifest(doc,'test','settings-example')['data']
            runtime.dispatch('approve_manifest',stage,human=True)
            first=runtime.dispatch('search',{'service':stage['service'],'limit':200})
            second=runtime.dispatch('search',{'service':stage['service'],'limit':200,'offset':200})
            self.assertIsInstance(first['data'],list)
            self.assertEqual(len(first['data']),200);self.assertEqual(len(second['data']),30)
            self.assertEqual([x['operation'] for x in first['data']+second['data']],[f'op_{i:03d}' for i in range(230)])
            self.assertEqual(len(runtime.dispatch('search',{})['data']),20)
            for bad in ({'limit':201},{'limit':0},{'limit':True},{'offset':-1},{'offset':False},{'offset':10_000_001},{'query':[]},{'query':'a'*201}):
                self.assertEqual(runtime.dispatch('search',bad)['status'],'invalid_arguments',bad)


if __name__=='__main__':unittest.main()
