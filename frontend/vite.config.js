import {defineConfig} from 'vite';
import vue from '@vitejs/plugin-vue';
const localProxy={target:'http://127.0.0.1:8080',configure(proxy){proxy.on('proxyReq',(outgoing,request)=>{if(request.headers.origin==='http://127.0.0.1:5173')outgoing.setHeader('Origin','http://127.0.0.1:8080');});}};
export default defineConfig({plugins:[vue()],base:'/app/',build:{outDir:'../web/app',emptyOutDir:true},server:{port:5173,proxy:{'/v1':localProxy,'/api':localProxy,'/health':localProxy}}});
