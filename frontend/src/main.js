import {createApp} from 'vue';
import {createRouter,createWebHashHistory} from 'vue-router';
import App from './App.vue';
import './style.css';
const router=createRouter({history:createWebHashHistory(),routes:[{path:'/',redirect:'/review'},...['review','runs','trace','monitor','policy','evaluation','settings'].map(p=>({path:'/'+p,component:{template:'<div />'}}))]});
createApp(App).use(router).mount('#app');
